"""
╔══════════════════════════════════════════════════════════════════════════════╗
║            UE5 BAKE PIPELINE  —  Blender Add-on                             ║
║            by Z3r0C00l / Dynart Interactive  —  v1.0.0                      ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  INSTALL:                                                                    ║
║    Edit → Preferences → Add-ons → Install → pick this .py file              ║
║    Enable "Render: UE5 Bake Pipeline"                                        ║
║                                                                              ║
║  USAGE:                                                                      ║
║    Properties → Render tab → "UE5 Bake Pipeline" section                    ║
║    Select mesh objects (or bakes all meshes if nothing selected)             ║
║    Set output folder, resolution, options → click BAKE FOR UE5              ║
║                                                                              ║
║  OUTPUT FILES:                                                               ║
║    T_Name_D.png        Diffuse / Albedo           sRGB                       ║
║    T_Name_N.png        Normal map (DirectX/UE5)   Non-Color                  ║
║    T_Name_ORM.png      R=AO  G=Roughness  B=Metal Non-Color                  ║
║                                                                              ║
║    With UDIM enabled:                                                        ║
║    T_Name_D.<UDIM>.png → T_Name_D.1001.png, T_Name_D.1002.png ...           ║
║                                                                              ║
║  UE5 IMPORT SETTINGS:                                                        ║
║    T_*_D   →  sRGB ON,  Compression: Default                                 ║
║    T_*_N   →  sRGB OFF, Compression: Normal Map                              ║
║    T_*_ORM →  sRGB OFF, Compression: Masks                                   ║
║    UDIM    →  enable "Import as UDIM" in texture import dialog               ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

# ── bl_info ───────────────────────────────────────────────────────────────────
# All standard fields per Blender's official add-on guidelines.
# 'support' : OFFICIAL (bundled), COMMUNITY (3rd-party), TESTING (experimental)
# 'warning' : shown in the Preferences list; leave empty string when stable
# 'doc_url' : opens when user clicks the ? icon in Preferences
# 'tracker_url': opens when user clicks "Report a Bug" in Preferences

bl_info = {
    "name":        "UE5 Bake Pipeline",
    "author":      "Z3r0C00l / Dynart Interactive",
    "version":     (1, 0, 0),
    "blender":     (3, 6, 0),
    "location":    "Properties > Render > UE5 Bake Pipeline",
    "description": (
        "Auto UV-unwrap, bake Diffuse / Normal (DirectX) / ORM packed texture "
        "and export UE5-ready PBR textures. UDIM per-material tiles supported."
    ),
    "warning":     "",
    "doc_url":     "https://github.com/DynartInteractive/ue5-bake-pipeline",
    "tracker_url": "https://github.com/DynartInteractive/ue5-bake-pipeline/issues",
    "support":     "COMMUNITY",
    "category":    "Render",
}

import bpy
import os
import numpy as np
import bmesh

from bpy.props import StringProperty, EnumProperty, BoolProperty
from bpy.types import PropertyGroup, Panel, Operator

# Tag used to identify temporary bake target Image Texture nodes
_BAKE_TAG = "ue5_bake_target"


# ══════════════════════════════════════════════════════════════════════════════
#  PROPERTY GROUP
#  Stores all user-facing settings on the scene so they persist between
#  sessions. Accessed via context.scene.ue5_bake_settings.
# ══════════════════════════════════════════════════════════════════════════════

class UE5BakeSettings(PropertyGroup):
    """Persistent settings for the UE5 Bake Pipeline add-on."""

    output_dir: StringProperty(
        name        = "Output Folder",
        description = "Folder where baked PNG textures are saved",
        subtype     = "DIR_PATH",
        default     = "//baked_textures",
    )

    bake_res: EnumProperty(
        name        = "Resolution",
        description = "Bake texture resolution per tile",
        items = [
            ("1024", "1024 px",  "Fast, low-res preview"),
            ("2048", "2048 px",  "Good for secondary props"),
            ("4096", "4096 px",  "Recommended for hero assets"),
            ("8192", "8192 px",  "Ultra — very slow, high VRAM"),
        ],
        default = "4096",
    )

    use_udim: BoolProperty(
        name        = "UDIM  (per-material tiles)",
        description = (
            "Each material slot gets its own UDIM tile (1001, 1002 ...). "
            "UVs are auto-offset. Output uses <UDIM> token recognised by UE5. "
            "Fully compatible with procedural materials - they use Object space, "
            "not UV coordinates, so UDIM only affects the bake target layout"
        ),
        default = False,
    )

    use_gpu: BoolProperty(
        name        = "GPU Baking",
        description = "Use GPU for baking (faster). Disable if you hit VRAM limits",
        default     = True,
    )

    replace_mats: BoolProperty(
        name        = "Replace Materials",
        description = (
            "After baking, replace procedural materials with a clean "
            "Principled PBR preview material using the baked textures"
        ),
        default = True,
    )

    bake_diffuse: BoolProperty(
        name        = "Diffuse / Albedo",
        description = "Bake the Base Color channel",
        default     = True,
    )

    bake_normal: BoolProperty(
        name        = "Normal Map  (DirectX)",
        description = (
            "Bake tangent-space normal map and convert from "
            "Blender OpenGL (+Y) to Unreal Engine DirectX (-Y) convention"
        ),
        default = True,
    )

    bake_orm: BoolProperty(
        name        = "ORM  (AO . Roughness . Metal)",
        description = (
            "Bake and pack Ambient Occlusion (R), Roughness (G) and "
            "Metallic (B) into a single ORM texture - UE5 standard layout"
        ),
        default = True,
    )

    export_fbx: BoolProperty(
        name        = "Export FBX",
        description = (
            "Export each processed mesh as an FBX file to the output folder. "
            "Axis settings are pre-configured for Unreal Engine 5 "
            "(Forward: -X, Up: Z, scale 1.0)"
        ),
        default = False,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  PANEL
#  Naming convention: {SPACE}_{TYPE}_{name}
#    RENDER  = Properties > Render context
#    PT      = Panel Type
#  The bl_idname must match the class name exactly for panels in Blender 2.8+.
# ══════════════════════════════════════════════════════════════════════════════

class RENDER_PT_ue5_bake_pipeline(Panel):
    """UE5 Bake Pipeline - auto UV-unwrap and export PBR textures for Unreal Engine 5"""

    bl_label       = "UE5 Bake Pipeline"
    bl_idname      = "RENDER_PT_ue5_bake_pipeline"
    bl_space_type  = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context     = "render"
    bl_options     = {"DEFAULT_CLOSED"}

    def draw_header(self, context):
        self.layout.label(text="", icon="RENDER_STILL")

    def draw(self, context):
        layout   = self.layout
        settings = context.scene.ue5_bake_settings

        # ── Output ─────────────────────────────────────────────────────────
        box = layout.box()
        box.label(text="Output", icon="FILE_FOLDER")
        box.prop(settings, "output_dir", text="")

        # ── Settings ───────────────────────────────────────────────────────
        box = layout.box()
        box.label(text="Settings", icon="PREFERENCES")
        box.prop(settings, "bake_res")

        row = box.row(align=True)
        row.prop(settings, "use_gpu",      toggle=True, icon="RESTRICT_RENDER_OFF")
        row.prop(settings, "replace_mats", toggle=True, icon="MATERIAL")

        # UDIM toggle + contextual hint
        col = box.column(align=True)
        col.prop(settings, "use_udim", icon="UV")
        hint = col.box()
        hint.scale_y = 0.75
        if settings.use_udim:
            hint.label(text="Each material  ->  own tile",          icon="INFO")
            hint.label(text="1001 . 1002 . 1003 ...",               icon="BLANK1")
            hint.label(text="UVs auto-offset per slot",              icon="BLANK1")
            hint.label(text="Works with procedural materials",       icon="CHECKMARK")
            hint.label(text="  Procedural nodes use Object space",   icon="BLANK1")
            hint.label(text="  UDIM is bake-target layout only",     icon="BLANK1")
        else:
            hint.label(text="All materials  ->  one atlas",          icon="INFO")
            hint.label(text="Tile 1001  (UV space 0 - 1)",           icon="BLANK1")
            hint.label(text="Good for simple / low-poly assets",     icon="BLANK1")

        # ── Channels ───────────────────────────────────────────────────────
        box = layout.box()
        box.label(text="Channels", icon="NODE_TEXTURE")
        col = box.column(align=True)
        col.prop(settings, "bake_diffuse", icon="COLOR")
        col.prop(settings, "bake_normal",  icon="NORMALS_FACE")
        col.prop(settings, "bake_orm",     icon="RNDCURVE")

        # ── Export ─────────────────────────────────────────────────────────
        box = layout.box()
        box.label(text="Export", icon="EXPORT")
        box.prop(settings, "export_fbx", icon="MOD_MESHDEFORM")
        if settings.export_fbx:
            hint = box.box()
            hint.scale_y = 0.75
            hint.label(text="FBX per mesh -> output folder",  icon="INFO")
            hint.label(text="Axis: Forward -X  Up Z  (UE5)",  icon="BLANK1")
            hint.label(text="Only selected or all meshes",    icon="BLANK1")

        # ── Target info ────────────────────────────────────────────────────
        selected   = [o for o in context.selected_objects if o.type == "MESH"]
        all_meshes = [o for o in context.scene.objects   if o.type == "MESH"]

        box = layout.box()
        if selected:
            box.label(text=f"{len(selected)} selected mesh(es)", icon="CHECKMARK")
        else:
            box.label(text=f"All {len(all_meshes)} mesh(es) in scene", icon="SCENE_DATA")

        # Warn if .blend unsaved and output uses relative path
        if settings.output_dir.startswith("//") and not bpy.data.filepath:
            warn = layout.box()
            warn.label(text="Save your .blend file first!", icon="ERROR")
            warn.label(text="// path requires a saved .blend", icon="BLANK1")

        # Warn if no channel selected - poll() will also grey out the button
        if not any([settings.bake_diffuse, settings.bake_normal, settings.bake_orm]):
            warn = layout.box()
            warn.label(text="No channels selected!", icon="ERROR")

        # ── Bake button ────────────────────────────────────────────────────
        layout.separator()
        row = layout.row()
        row.scale_y = 2.2
        row.operator(UE5_BAKE_OT_run.bl_idname,
                     text="  BAKE FOR UE5  ", icon="RENDER_STILL")


# ══════════════════════════════════════════════════════════════════════════════
#  OPERATOR
#  Naming convention: {ADDON}_{OT}_{name}
#    UE5_BAKE  = add-on prefix  (UPPER_CASE)
#    OT        = Operator Type
#    run       = operation name (lower_case)
#
#  bl_idname uses dot notation: "addon_prefix.operation_name" (all lowercase)
#
#  bl_options:
#    REGISTER  - always include; allows operator to appear in the info log
#    UNDO      - MANDATORY when modifying scene data. Creates an undo step
#                after execute() returns {'FINISHED'}. Without this, Blender's
#                undo stack is corrupted and the user cannot Ctrl+Z the bake.
# ══════════════════════════════════════════════════════════════════════════════

class UE5_BAKE_OT_run(Operator):
    """Bake procedural materials to UE5-ready PBR textures (Diffuse, Normal DX, ORM)"""

    bl_idname  = "ue5_bake.run"
    bl_label   = "Run UE5 Bake Pipeline"
    bl_options = {"REGISTER", "UNDO"}

    # ── poll ─────────────────────────────────────────────────────────────────
    # poll() is a classmethod called before execute(). If it returns False,
    # the button is greyed out and the operator cannot run.
    # Best practice: always validate the minimum required context here.

    @classmethod
    def poll(cls, context):
        has_mesh    = any(o.type == "MESH" for o in context.scene.objects)
        settings    = context.scene.ue5_bake_settings
        any_channel = any([
            settings.bake_diffuse,
            settings.bake_normal,
            settings.bake_orm,
        ])
        return has_mesh and any_channel

    # ── execute ──────────────────────────────────────────────────────────────

    def execute(self, context):
        settings = context.scene.ue5_bake_settings
        res      = int(settings.bake_res)
        use_udim = settings.use_udim

        # Output path validation:
        # If path uses // (relative to .blend) but the file has not been saved,
        # bpy.path.abspath resolves to Blenders install directory which is
        # write-protected on Windows -> WinError 5 Access Denied.
        raw_path = settings.output_dir
        if raw_path.startswith("//"):
            if not bpy.data.filepath:
                self.report(
                    {"ERROR"},
                    "Output path uses // (relative) but the .blend file has not "
                    "been saved yet. Save your .blend first, or set an absolute "
                    "path in the Output Folder field (e.g. C:\baked_textures)."
                )
                return {"CANCELLED"}

        out_dir = os.path.normpath(bpy.path.abspath(raw_path))

        try:
            os.makedirs(out_dir, exist_ok=True)
        except PermissionError:
            self.report(
                {"ERROR"},
                f"Access denied: cannot write to '{out_dir}'. "
                "Choose a folder you own, such as inside Documents or Desktop."
            )
            return {"CANCELLED"}
        except OSError as e:
            self.report({"ERROR"}, f"Could not create output folder: {e}")
            return {"CANCELLED"}

        if not os.access(out_dir, os.W_OK):
            self.report(
                {"ERROR"},
                f"Folder '{out_dir}' exists but is not writable. "
                "Check permissions or choose a different output path."
            )
            return {"CANCELLED"}

        # Switch to Cycles - baking is not available in EEVEE
        scene                = context.scene
        scene.render.engine  = "CYCLES"
        scene.cycles.device  = "GPU" if settings.use_gpu else "CPU"
        scene.cycles.samples = 1     # minimal for data-channel bakes

        # Collect targets: selected meshes, or all meshes if none selected
        targets = [o for o in context.selected_objects if o.type == "MESH"]
        if not targets:
            targets = [o for o in scene.objects if o.type == "MESH"]

        # Should never reach here due to poll(), but guard defensively
        if not targets:
            self.report({"WARNING"}, "No mesh objects found in scene.")
            return {"CANCELLED"}

        # Count total bake calls for progress bar
        n_channels = (
            (1 if settings.bake_diffuse else 0) +
            (1 if settings.bake_normal  else 0) +
            (3 if settings.bake_orm     else 0)   # roughness + metallic + AO
        )
        total_steps = len(targets) * n_channels + 1
        step        = 0
        wm          = context.window_manager
        wm.progress_begin(0, total_steps)

        self._set_status(context, "UE5 Bake Pipeline - starting ...")

        try:
            for obj in targets:
                safe = obj.name.replace(" ", "_").replace(".", "_")
                self._set_status(context, f"Processing: {obj.name}")

                # Isolate this object as the sole active/selected object
                bpy.ops.object.select_all(action="DESELECT")
                obj.select_set(True)
                context.view_layer.objects.active = obj

                # Ensure all materials use nodes
                for mat in obj.data.materials:
                    if mat:
                        mat.use_nodes = True

                mat_count = max(len(obj.data.materials), 1)

                # ── UV UNWRAP ────────────────────────────────────────────
                if use_udim:
                    self._unwrap_udim(obj)
                else:
                    self._unwrap_standard(obj)

                # ── IMAGE BUFFERS ────────────────────────────────────────
                img_d  = self._new_img(f"{safe}_D_raw",  res, False, use_udim, mat_count) if settings.bake_diffuse else None
                img_r  = self._new_img(f"{safe}_R_raw",  res, True,  use_udim, mat_count) if settings.bake_orm     else None
                img_m  = self._new_img(f"{safe}_M_raw",  res, True,  use_udim, mat_count) if settings.bake_orm     else None
                img_ao = self._new_img(f"{safe}_AO_raw", res, True,  use_udim, mat_count) if settings.bake_orm     else None
                img_n  = self._new_img(f"{safe}_N_raw",  res, True,  use_udim, mat_count) if settings.bake_normal  else None

                # Add bake target Image Texture nodes to all materials
                for mat in obj.data.materials:
                    if not mat or not mat.use_nodes:
                        continue
                    for img in filter(None, [img_d, img_r, img_m, img_ao, img_n]):
                        self._add_bake_node(mat, img)

                orm_img = None

                # ── DIFFUSE ──────────────────────────────────────────────
                # Wire Base Color -> Emission -> Output, bake EMIT.
                # This gives pure unlit color with zero lighting dependency,
                # so it works correctly even on scenes with no lights.
                if settings.bake_diffuse:
                    self._set_status(context, f"{obj.name} - baking Diffuse ...")
                    restore_d = self._setup_emit_bake(obj, "Base Color")
                    self._activate_bake_nodes(obj, img_d)
                    bpy.ops.object.bake(type="EMIT", margin=16, use_clear=True)
                    self._restore_emit_bake(restore_d)
                    self._save_img(img_d, out_dir, f"T_{safe}_D", use_udim)
                    step += 1; wm.progress_update(step); bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)

                # ── ROUGHNESS ────────────────────────────────────────────
                if settings.bake_orm:
                    self._set_status(context, f"{obj.name} - baking Roughness ...")
                    self._activate_bake_nodes(obj, img_r)
                    bpy.ops.object.bake(type="ROUGHNESS", margin=16, use_clear=True)
                    step += 1; wm.progress_update(step); bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)

                    # ── METALLIC (Emission trick) ─────────────────────────
                    # Blender has no METALLIC bake type. Same Emission trick
                    # as Diffuse: wire Metallic -> Emission -> Output, bake EMIT.
                    self._set_status(context, f"{obj.name} - baking Metallic ...")
                    restore_m = self._setup_emit_bake(obj, "Metallic")
                    self._activate_bake_nodes(obj, img_m)
                    bpy.ops.object.bake(type="EMIT", margin=16, use_clear=True)
                    self._restore_emit_bake(restore_m)
                    step += 1; wm.progress_update(step); bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)

                    # ── AO ────────────────────────────────────────────────
                    # AO needs more samples than other channels for quality
                    self._set_status(context, f"{obj.name} - baking AO ...")
                    orig_samples         = scene.cycles.samples
                    scene.cycles.samples = 64
                    self._activate_bake_nodes(obj, img_ao)
                    bpy.ops.object.bake(type="AO", margin=16, use_clear=True)
                    scene.cycles.samples = orig_samples
                    step += 1; wm.progress_update(step); bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)

                    # ── PACK ORM ──────────────────────────────────────────
                    self._set_status(context, f"{obj.name} - packing ORM ...")
                    orm_img = self._pack_orm(safe, res, img_ao, img_r, img_m,
                                             use_udim, mat_count)
                    self._save_img(orm_img, out_dir, f"T_{safe}_ORM", use_udim)

                # ── NORMAL ───────────────────────────────────────────────
                if settings.bake_normal:
                    self._set_status(context, f"{obj.name} - baking Normal ...")
                    self._activate_bake_nodes(obj, img_n)
                    bpy.ops.object.bake(
                        type="NORMAL",
                        normal_space="TANGENT",
                        margin=16,
                        use_clear=True,
                    )
                    # Convert OpenGL (+Y) -> DirectX (-Y) for UE5
                    self._flip_normal_green(img_n, use_udim, mat_count)
                    self._save_img(img_n, out_dir, f"T_{safe}_N", use_udim)
                    step += 1; wm.progress_update(step); bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)

                # ── CLEANUP BAKE NODES ────────────────────────────────────
                for mat in obj.data.materials:
                    if mat and mat.use_nodes:
                        self._remove_bake_nodes(mat)

                # ── REPLACE MATERIALS ─────────────────────────────────────
                if settings.replace_mats:
                    pbr = self._build_preview_mat(
                        safe,
                        img_d   if settings.bake_diffuse else None,
                        img_n   if settings.bake_normal  else None,
                        orm_img if settings.bake_orm     else None,
                    )
                    obj.data.materials.clear()
                    obj.data.materials.append(pbr)

                # ── FBX EXPORT ────────────────────────────────────────────
                if settings.export_fbx:
                    self._set_status(context, f"{obj.name} - exporting FBX ...")
                    self._export_fbx(obj, out_dir, safe)

        except Exception as e:
            # Always clean up status bar on error
            self._clear_status(context)
            wm.progress_end()
            self.report({"ERROR"}, f"Bake pipeline failed: {e}")
            return {"CANCELLED"}

        wm.progress_update(total_steps)
        wm.progress_end()

        # Always clear status bar text when operator finishes
        self._clear_status(context)

        self.report({"INFO"}, f"UE5 Bake complete -> {out_dir}")
        return {"FINISHED"}


    # ══════════════════════════════════════════════════════════════════════════
    #  PRIVATE HELPERS
    #  Prefixed with _ per Python / Blender convention for internal methods.
    # ══════════════════════════════════════════════════════════════════════════

    def _set_status(self, context, text):
        """
        Update Blender status bar text and force an immediate UI redraw.

        Without the redraw_timer flush, the operator blocks the main thread
        so status_text_set() calls are queued but never painted until the
        operator returns - making the progress bar invisible during baking.
        DRAW_WIN_SWAP flushes all pending redraws synchronously.
        """
        context.workspace.status_text_set(text)
        bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)

    def _clear_status(self, context):
        """Reset status bar to Blender default. Call on finish and on error."""
        context.workspace.status_text_set(None)
        bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)

    # ── Image management ──────────────────────────────────────────────────────

    def _new_img(self, name, res, is_data, udim, tile_count):
        """Create a blank bake target image, optionally as a UDIM tiled source."""
        if name in bpy.data.images:
            bpy.data.images.remove(bpy.data.images[name])
        img = bpy.data.images.new(
            name, width=res, height=res, alpha=False, is_data=is_data
        )
        img.colorspace_settings.name = "Non-Color" if is_data else "sRGB"
        if udim and tile_count > 1:
            img.source = "TILED"
            for i in range(1, tile_count):
                img.tiles.new(tile_number=1001 + i)
        return img

    def _save_img(self, img, out_dir, base_name, use_udim):
        """Save image to disk. UDIM images use the <UDIM> token in filename."""
        token    = ".<UDIM>" if (use_udim and img.source == "TILED") else ""
        filepath = os.path.join(out_dir, f"{base_name}{token}.png")
        img.filepath_raw = filepath
        img.file_format  = "PNG"
        img.save()

    # ── Bake node management ─────────────────────────────────────────────────

    def _add_bake_node(self, mat, img):
        """Add an unlinked Image Texture node tagged as the bake target."""
        node       = mat.node_tree.nodes.new("ShaderNodeTexImage")
        node.image = img
        node.name  = node.label = _BAKE_TAG

    def _remove_bake_nodes(self, mat):
        """Remove all bake target nodes from a material."""
        for n in [n for n in mat.node_tree.nodes if n.name == _BAKE_TAG]:
            mat.node_tree.nodes.remove(n)

    def _activate_bake_nodes(self, obj, img):
        """Select and activate the bake target node matching img in every material."""
        for mat in obj.data.materials:
            if not mat or not mat.use_nodes:
                continue
            nodes = mat.node_tree.nodes
            for n in nodes:
                n.select = False
            for n in nodes:
                if n.name == _BAKE_TAG and n.image == img:
                    n.select     = True
                    nodes.active = n
                    break

    # ── UV unwrap: standard ──────────────────────────────────────────────────

    def _unwrap_standard(self, obj):
        """Smart UV Project all geometry into the 0-1 UV space (tile 1001)."""
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.smart_project(
            angle_limit    = 1.15,   # ~66 degrees - good balance for hard-surface
            island_margin  = 0.02,
            correct_aspect = True,
        )
        bpy.ops.object.mode_set(mode="OBJECT")

    # ── UV unwrap: UDIM ──────────────────────────────────────────────────────
    # For each material index i, Smart-UV-project that material's faces into
    # the 0-1 space, then offset UVs into the correct UDIM tile:
    #   tile 1001 -> U+0, V+0   (no offset - stays in 0-1)
    #   tile 1002 -> U+1, V+0
    #   tile 1003 -> U+2, V+0   ...
    #   tile 1011 -> U+0, V+1   (wraps every 10 tiles - UDIM convention)

    def _unwrap_udim(self, obj):
        """Smart UV Project per material slot, offset UVs into UDIM tiles."""
        mesh = obj.data
        if not mesh.uv_layers:
            mesh.uv_layers.new(name="UDIMMap")
        uv_layer_name = mesh.uv_layers.active.name

        for mat_idx in range(len(mesh.materials)):
            bpy.ops.object.mode_set(mode="OBJECT")
            for poly in mesh.polygons:
                poly.select = (poly.material_index == mat_idx)

            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.uv.smart_project(
                angle_limit    = 1.15,
                island_margin  = 0.02,
                correct_aspect = True,
            )
            bpy.ops.object.mode_set(mode="OBJECT")

            u_off = mat_idx % 10
            v_off = mat_idx // 10
            if u_off == 0 and v_off == 0:
                continue   # tile 1001 - no offset needed

            bm       = bmesh.new()
            bm.from_mesh(mesh)
            uv_layer = bm.loops.layers.uv[uv_layer_name]

            for face in bm.faces:
                if face.material_index == mat_idx:
                    for loop in face.loops:
                        loop[uv_layer].uv.x += u_off
                        loop[uv_layer].uv.y += v_off

            bm.to_mesh(mesh)
            bm.free()

        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.object.mode_set(mode="OBJECT")

    # ── Metallic emission trick ──────────────────────────────────────────────

    def _setup_emit_bake(self, obj, socket_name):
        """
        Generic Emission trick for any Principled BSDF input socket.

        Temporarily wires the given socket (e.g. "Base Color", "Metallic",
        "Roughness") into an Emission node connected to the Material Output,
        then baking EMIT captures that channel as a pure unlit texture with
        no lighting dependency. Works correctly on scenes with no lights.

        Returns restore data that must be passed to _restore_emit_bake().
        """
        restore = []
        for mat in obj.data.materials:
            if not mat or not mat.use_nodes:
                continue
            nodes  = mat.node_tree.nodes
            links  = mat.node_tree.links
            pbsdf  = next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), None)
            output = next(
                (n for n in nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output),
                next((n for n in nodes if n.type == "OUTPUT_MATERIAL"), None)
            )
            if not pbsdf or not output:
                continue

            # Detach the current Surface link
            orig_socket = None
            for link in list(links):
                if link.to_node == output and link.to_socket.name == "Surface":
                    orig_socket = link.from_socket
                    links.remove(link)
                    break

            # Create temporary Emission node
            emit              = nodes.new("ShaderNodeEmission")
            emit.name         = emit.label = "ue5_bake_temp_emit"
            emit.inputs["Strength"].default_value = 1.0

            # Wire the requested socket into Emission Color
            src = pbsdf.inputs[socket_name]
            if src.is_linked:
                links.new(src.links[0].from_socket, emit.inputs["Color"])
            else:
                # Scalar inputs (Metallic, Roughness) need broadcasting to RGB
                raw = src.default_value
                if hasattr(raw, "__len__"):
                    # Already an RGB/RGBA value (e.g. Base Color)
                    emit.inputs["Color"].default_value = (raw[0], raw[1], raw[2], 1.0)
                else:
                    # Scalar - broadcast to grey
                    emit.inputs["Color"].default_value = (raw, raw, raw, 1.0)

            links.new(emit.outputs["Emission"], output.inputs["Surface"])
            restore.append({
                "mat": mat, "emit": emit,
                "output": output, "orig": orig_socket,
            })
        return restore

    def _restore_emit_bake(self, restore):
        """Undo _setup_emit_bake() wiring and remove all temp Emission nodes."""
        for e in restore:
            mat, emit, output, orig = e["mat"], e["emit"], e["output"], e["orig"]
            links = mat.node_tree.links
            for link in list(links):
                if link.to_node == output and link.from_node == emit:
                    links.remove(link)
            for link in list(links):
                if link.to_node == emit:
                    links.remove(link)
            mat.node_tree.nodes.remove(emit)
            if orig:
                links.new(orig, output.inputs["Surface"])

    # ── ORM packing ──────────────────────────────────────────────────────────
    # UE5 standard ORM layout:
    #   R = Ambient Occlusion  (1.0 = fully lit)
    #   G = Roughness
    #   B = Metallic

    def _pack_orm(self, safe_name, res, img_ao, img_r, img_m, use_udim, tile_count):
        """Combine AO / Roughness / Metallic into a single ORM image."""
        name = f"T_{safe_name}_ORM_buf"
        if name in bpy.data.images:
            bpy.data.images.remove(bpy.data.images[name])

        orm = bpy.data.images.new(name, width=res, height=res,
                                   alpha=False, is_data=True)
        orm.colorspace_settings.name = "Non-Color"

        if use_udim and tile_count > 1:
            orm.source = "TILED"
            for i in range(1, tile_count):
                orm.tiles.new(tile_number=1001 + i)
            for tile in orm.tiles:
                orm.tiles.active = tile
                for src in [img_ao, img_r, img_m]:
                    if src:
                        src.tiles.active = next(
                            (t for t in src.tiles if t.number == tile.number),
                            src.tiles[0],
                        )
                self._pack_orm_pixels(orm, img_ao, img_r, img_m, res)
        else:
            self._pack_orm_pixels(orm, img_ao, img_r, img_m, res)

        return orm

    def _pack_orm_pixels(self, orm_img, img_ao, img_r, img_m, res):
        """Write R=AO, G=Roughness, B=Metallic pixel data using numpy."""
        n = res * res

        def gray(img, fallback):
            if img is None:
                return np.full(n, fallback, dtype=np.float32)
            px = np.array(img.pixels[:], dtype=np.float32)
            return px[0::4]  # Red channel (identical to G/B for grayscale bakes)

        ao = gray(img_ao, 1.0)
        rg = gray(img_r,  0.5)
        mt = gray(img_m,  0.0)

        out       = np.ones(n * 4, dtype=np.float32)
        out[0::4] = ao   # R = AO
        out[1::4] = rg   # G = Roughness
        out[2::4] = mt   # B = Metallic

        orm_img.pixels = out.tolist()

    # ── Normal map: OpenGL -> DirectX ────────────────────────────────────────
    # Blender: tangent-space OpenGL  (+Y = up in UV space)
    # UE5:     tangent-space DirectX (-Y = up in UV space)
    # Fix: invert the Green channel.

    def _flip_normal_green(self, img_n, use_udim, tile_count):
        """Invert Green channel in-place (OpenGL -> DirectX for UE5)."""
        if use_udim and tile_count > 1:
            for tile in img_n.tiles:
                img_n.tiles.active = tile
                self._invert_green(img_n)
        else:
            self._invert_green(img_n)

    def _invert_green(self, img):
        # After bpy.ops.object.bake(), pixel data lives in GPU memory.
        # Accessing img.pixels directly can return stale/zeroed CPU data,
        # causing 1.0 - 0.0 = 1.0 on the G channel -> completely green result.
        # Forcing a pixel read via update() + an explicit pack/unpack cycle
        # flushes GPU -> CPU before numpy touches the buffer.
        img.update()
        # Accessing element [0] forces a full GPU->CPU pixel sync in Blender
        _ = img.pixels[0]
        px        = np.array(img.pixels[:], dtype=np.float32)
        px[1::4]  = 1.0 - px[1::4]
        img.pixels = px.tolist()
        img.update()

    # ── Preview PBR material ──────────────────────────────────────────────────

    def _build_preview_mat(self, name, img_d, img_n, img_orm):
        """
        Build a Blender Principled PBR preview material from baked textures.
        ORM is unpacked via Separate RGB. This is a viewport preview only -
        the canonical material lives in UE5.
        """
        mat = bpy.data.materials.new(name=f"T_{name}_PBR_preview")
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        nodes.clear()

        out   = nodes.new("ShaderNodeOutputMaterial"); out.location   = (700, 0)
        pbsdf = nodes.new("ShaderNodeBsdfPrincipled"); pbsdf.location = (300, 0)
        links.new(pbsdf.outputs["BSDF"], out.inputs["Surface"])

        def add_tex(img, x, y, cs):
            n = nodes.new("ShaderNodeTexImage")
            n.image = img
            n.image.colorspace_settings.name = cs
            n.location = (x, y)
            return n

        if img_d:
            nd = add_tex(img_d, -500, 400, "sRGB")
            links.new(nd.outputs["Color"], pbsdf.inputs["Base Color"])

        if img_orm:
            no  = add_tex(img_orm, -800, 0, "Non-Color")
            sep = nodes.new("ShaderNodeSeparateRGB"); sep.location = (-400, 0)
            links.new(no.outputs["Color"],  sep.inputs["Image"])
            links.new(sep.outputs["G"],     pbsdf.inputs["Roughness"])
            links.new(sep.outputs["B"],     pbsdf.inputs["Metallic"])

        if img_n:
            nn   = add_tex(img_n, -800, -400, "Non-Color")
            nmap = nodes.new("ShaderNodeNormalMap"); nmap.location = (-400, -400)
            links.new(nn.outputs["Color"],    nmap.inputs["Color"])
            links.new(nmap.outputs["Normal"], pbsdf.inputs["Normal"])

        return mat

    # ── FBX export ───────────────────────────────────────────────────────────

    def _export_fbx(self, obj, out_dir, safe_name):
        """
        Export a single mesh object as FBX with UE5-correct axis settings.

        UE5 coordinate system:  Forward = X,  Right = Y,  Up = Z
        Blender coordinate system: Forward = -Y, Up = Z

        bake_space_transform=True lets Blender bake the axis correction into
        the mesh data so the FBX arrives in UE5 with correct orientation
        without needing to rotate it in the import dialog.

        use_selection=True exports only the active object. We isolate the
        object before calling this so it is always the only selected mesh.
        """
        fbx_path = os.path.join(out_dir, f"{safe_name}.fbx")
        bpy.ops.export_scene.fbx(
            filepath             = fbx_path,
            use_selection        = True,       # only the current object
            apply_unit_scale     = True,
            apply_scale_options  = "FBX_SCALE_NONE",
            bake_space_transform = True,       # bake axis correction into mesh
            mesh_smooth_type     = "FACE",
            use_mesh_modifiers   = True,
            axis_forward         = "-X",       # UE5: Forward = X axis
            axis_up              = "Z",        # UE5: Up = Z axis
            path_mode            = "COPY",
            embed_textures       = False,      # textures are separate PNGs
            add_leaf_bones       = False,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  REGISTER / UNREGISTER
#
#  Blender 2.8+ best practices applied:
#
#  1. 'classes' tuple (not the removed bpy.utils.register_module)
#  2. Register in forward order, unregister in reversed() order
#  3. PointerProperty assigned AFTER its PropertyGroup class is registered
#  4. PointerProperty deleted in unregister() BEFORE unregistering the class
#     to avoid memory leaks and errors on reload
#
#  Alternative: bpy.utils.register_classes_factory(classes) generates the
#  register/unregister functions automatically, but does not handle
#  PointerProperty cleanup, so the explicit loop is used here.
# ══════════════════════════════════════════════════════════════════════════════

classes = (
    UE5BakeSettings,
    RENDER_PT_ue5_bake_pipeline,
    UE5_BAKE_OT_run,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    # PointerProperty must be registered after its PropertyGroup class
    bpy.types.Scene.ue5_bake_settings = bpy.props.PointerProperty(
        type=UE5BakeSettings
    )


def unregister():
    # Delete PointerProperty before unregistering its class to avoid leaks
    del bpy.types.Scene.ue5_bake_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
