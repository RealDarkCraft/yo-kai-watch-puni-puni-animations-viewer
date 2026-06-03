import struct
import math
import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk
import pygame
import pygame.locals as pgl

# ─────────────────────────────────────────────
#  READER & DATA CLASSES
# ─────────────────────────────────────────────

class Reader:
    def __init__(self, fp):
        self.f = open(fp, 'rb')

    def read(self, length):
        return self.f.read(length)

    def read_uint8(self):
        data = self.f.read(1)
        if not data:
            return None
        return struct.unpack('>B', data)[0]

    def read_uint16(self):
        data = self.f.read(2)
        if not data:
            return None
        return struct.unpack('>H', data)[0]

    def tell(self):
        return self.f.tell()

    def close(self):
        self.f.close()
        self.f = None

    def seek(self, val):
        self.f.seek(val)

    def read_string_16(self):
        length = self.read_uint16()
        if not length or length == 0:
            return ""
        return self.f.read(length).decode('shift-jis', errors='ignore')

    def read_vec2(self):
        return struct.unpack('>2f', self.f.read(8))

    def read_color(self):
        return struct.unpack('>4B', self.f.read(4))


class PuniFlashImage:
    class Layer:
        class FrameInfo:
            def toObj(self):
                return {
                    "index": self.frameIndex,
                    "pos": self.pos,
                    "scale": self.scale,
                    "skew": self.skew,
                    "anchor": self.anchor,
                    "color": self.color,
                    "blend": self.blend,
                }

            def __init__(self, fp):
                self.reader = fp
                self.frameIndex = self.reader.read_uint16()
                self.pos = self.reader.read_vec2()
                self.scale = self.reader.read_vec2()
                self.skew = self.reader.read_vec2()
                self.anchor = self.reader.read_vec2()
                self.color = self.reader.read_color()
                self.blend = self.reader.read_uint8()

        def toObj(self):
            return {
                "groupId": self.groupId,
                "layer_name": self.layerName,
                "texture": self.texturePath,
                "frames": [x.toObj() for x in self.frames],
            }

        def __init__(self, fp):
            self.reader = fp
            self.layerName = self.reader.read_string_16()
            self.groupId = self.reader.read_uint16()
            self.texturePath = self.reader.read_string_16()
            self.frameCount = self.reader.read_uint16()
            self.frames = []
            for _ in range(self.frameCount):
                self.frames.append(self.FrameInfo(self.reader))

    def toObj(self):
        return {
            "version": self.version,
            "fps": self.fps,
            "size": {"width": self.width, "height": self.height},
            "layers": [x.toObj() for x in self.layers],
        }

    def open(self, file):
        self.reader = Reader(file)
        self.version = self.reader.read_uint16()
        self.fps = self.reader.read_uint16()
        self.width = self.reader.read_uint16()
        self.height = self.reader.read_uint16()
        self.layerCount = self.reader.read_uint16()
        self.layers = []
        for _ in range(self.layerCount):
            self.layers.append(self.Layer(self.reader))
        self.reader.close()
        return self


# ─────────────────────────────────────────────
#  TEXTURE CACHE
# ─────────────────────────────────────────────

class TextureCache:
    def __init__(self, base_dir, datpath):
        self.base_dir = base_dir
        self.dat_dir = os.path.dirname(datpath)
        self._cache: dict[str, pygame.Surface | None] = {}

    def get(self, path: str) -> pygame.Surface | None:
        if path in self._cache:
            return self._cache[path]
        if path == "":
            self._cache[path] = None
            return None
            
        # we do this currently to test, because we don't know yet how to properly found/search textures
        candidates = [os.path.join(self.dat_dir, path)]
        #path = path.replace("symbol/", self.symbol)
        for i in ["data", "event", "gacha", "image", "map", "movie", "particle", "skill", "sound", "youkai", "image\puzzleBoss"]:
            candidates.append(os.path.join(self.base_dir, path.replace("symbol/", f"{i}/")))
        found = False
        for c in candidates:
            if os.path.isfile(c):
                try:
                    img = Image.open(c).convert("RGBA")
                    surf = pygame.image.frombuffer(
                        img.tobytes(),
                        img.size,
                        "RGBA"
                    ).convert_alpha()
                    self._cache[path] = surf
                    found = True
                    return surf
                except Exception as e:
                    print(f"[TextureCache] Cannot load {c}: {e}")
        if found == False:
            print(f"[TextureCache] Cannot load {path}")
        self._cache[path] = None
        return None


# ─────────────────────────────────────────────
#  PYGAME RENDERER
# ─────────────────────────────────────────────

BG_COLOR = (30, 30, 40)
UI_COLOR = (20, 20, 28)
ACCENT   = (100, 200, 255)
TEXT_COL = (220, 220, 230)

class Renderer:
    def __init__(self, anim: PuniFlashImage, asset_dir: str, symbol: str):
        self.anim = anim
        self.cache = TextureCache(asset_dir, symbol)
        self.frame = 0
        self.total_frames = self._compute_total_frames()
        self.playing = True
        self.speed = 1.0

        self.layer_visible: dict[int, bool] = {i: True for i in range(len(anim.layers))}
        self._lock = threading.Lock()

        # Cache de surfaces transformées : {(layer_idx, frame_idx): (surface, draw_offset_x, draw_offset_y)}
        # draw_offset = décalage depuis fi.pos jusqu'au coin top-left de la surface
        self._transform_cache: dict[tuple, tuple] = {}
        

    def _precache_all(self):
        for layer_idx, layer in enumerate(self.anim.layers):
            if (layer.layerName[0] == "!"):
                print("Warning : unk layer type")
            elif (layer.layerName[0] == "%"):
                print(f"Warning : layer {layer.layerName} is referencing an .split")
            surf = self.cache.get(layer.texturePath)
            if surf is None:
                continue
            for fi in layer.frames:
                key = (layer_idx, fi.frameIndex)
                result = self._compute_transform(surf, fi)
                if result is not None:
                    tinted, off_x, off_y = result
                    # Précalcule aussi la version add si flag=1
                    if fi.blend == 1:
                        add_surf = tinted.copy()
                        import numpy as np
                        arr = pygame.surfarray.pixels3d(add_surf)
                        alp = pygame.surfarray.pixels_alpha(tinted)
                        arr[:] = (arr.astype(np.uint16) * alp[:,:,None] // 255).clip(0,255).astype(np.uint8)
                        del arr, alp
                        pygame.surfarray.pixels_alpha(add_surf)[:] = 0
                        self._transform_cache[key] = (tinted, off_x, off_y, add_surf)
                    else:
                        if fi.blend != 0:
                            print("Unk flag {fi.blend}")
                        self._transform_cache[key] = (tinted, off_x, off_y, None)

    def _compute_transform(self, surf, fi):
        """Retourne (surface_transformée, offset_x, offset_y) depuis fi.pos."""
        tw, th = surf.get_size()
        sx, sy = fi.scale
        skx, sky = fi.skew
        ax, ay = fi.anchor

        # Scale nulle = invisible
        if abs(sx) < 1e-6 or abs(sy) < 1e-6:
            return None

        apx = ax * tw
        apy = ay * th

        skx_r = math.radians(skx)
        sky_r = math.radians(sky)
        a =  sx * math.cos(sky_r)
        b = -sy * math.sin(skx_r)
        c =  sx * math.sin(sky_r)
        d =  sy * math.cos(skx_r)

        det = a * d - b * c
        flip_needed = det < 0

        if flip_needed:
            surf_work = pygame.transform.flip(surf, False, True)
            b2, d2 = -b, -d
        else:
            surf_work = surf
            b2, d2 = b, d

        scale_x = math.sqrt(a * a + c * c)
        scale_y = math.sqrt(b2 * b2 + d2 * d2)
        angle_deg = -math.degrees(math.atan2(c, a))

        new_w = max(1, int(scale_x * tw))
        new_h = max(1, int(scale_y * th))
        try:
            transformed = pygame.transform.smoothscale(surf_work, (new_w, new_h))
        except Exception:
            return None

        a_col, r, g, b_col = fi.color
        tinted = transformed.copy()
        tinted.fill((r, g, b_col, a_col), special_flags=pygame.BLEND_RGBA_MULT)

        if abs(angle_deg) > 0.01:
            tinted = pygame.transform.rotozoom(tinted, angle_deg, 1.0)

        rot_w, rot_h = tinted.get_size()

        scaled_apx = apx * (new_w / tw) if tw > 0 else 0
        scaled_apy = apy * (new_h / th) if th > 0 else 0

        if flip_needed:
            scaled_apy = new_h - scaled_apy

        if abs(angle_deg) > 0.01:
            dx = scaled_apx - new_w / 2.0
            dy = scaled_apy - new_h / 2.0
            rad = math.radians(-angle_deg)
            cos_a = math.cos(rad)
            sin_a = math.sin(rad)
            rot_dx = dx * cos_a - dy * sin_a
            rot_dy = dx * sin_a + dy * cos_a
            off_x = -(rot_w / 2.0 + rot_dx)
            off_y = -(rot_h / 2.0 + rot_dy)
        else:
            off_x = -scaled_apx
            off_y = -scaled_apy

        return (tinted, off_x, off_y)

    def _compute_total_frames(self) -> int:
        total = 1
        for layer in self.anim.layers:
            for fi in layer.frames:
                total = max(total, fi.frameIndex + 1)
        return total

    def toggle_layer(self, idx: int, visible: bool):
        with self._lock:
            self.layer_visible[idx] = visible

    def _get_frame_info(self, layer, current_frame, group_layers):
        best = None
        for fi in layer.frames:
            if fi.frameIndex <= current_frame:
                if best is None or fi.frameIndex > best.frameIndex:
                    best = fi

        if best is None:
            return None

        last_fi = max(layer.frames, key=lambda f: f.frameIndex)
        if best.frameIndex == last_fi.frameIndex and current_frame > last_fi.frameIndex:
            if last_fi.frameIndex < self.total_frames - 1:
                
                # --- NOUVELLE VÉRIFICATION DU GROUPE ---
                # On vérifie si AUCUNE couche du groupe n'a encore de frames à afficher.
                # 'all_finished' sera True si toutes les couches du groupe ont dépassé leur dernière frame.
                all_finished = True
                for gl in group_layers:
                    if gl.frames:  # Sécurité si une couche n'a pas de frames
                        gl_last = max(gl.frames, key=lambda f: f.frameIndex)
                        # Si une seule couche du groupe n'a PAS encore dépassé sa dernière frame :
                        if current_frame <= gl_last.frameIndex:
                            all_finished = False
                            break
                
                # On ne cache le layer (return None) que si TOUT le groupe est terminé
                if all_finished:
                    return None
                
                # Sinon, si le groupe est encore actif, on laisse le code continuer 
                # pour retourner la "best" frame (la dernière frame connue)
                pass 
                
        return best

    def _render_layer(self, screen, layer_idx, layer, group_layers):
        with self._lock:
            if not self.layer_visible.get(layer_idx, True):
                return

        fi = self._get_frame_info(layer, int(self.frame), group_layers)
        if fi is None:
            return

        key = (layer_idx, fi.frameIndex)
        result = self._transform_cache.get(key)
        if result is None:
            return

        tinted, off_x, off_y, add_surf = result
        tx, ty = fi.pos

        if fi.blend == 1 and add_surf is not None:
            screen.blit(add_surf, (tx + off_x, ty + off_y), special_flags=pygame.BLEND_RGB_ADD)
        else:
            screen.blit(tinted, (tx + off_x, ty + off_y))

    def draw_hud(self, screen, font_sm):
        w, h = screen.get_size()
        bar_h = 36
        pygame.draw.rect(screen, UI_COLOR, (0, h - bar_h, w, bar_h))
        bar_x, bar_y = 10, h - bar_h + 8
        bar_w = w - 120
        bar_total = max(1, self.total_frames - 1)
        filled = int((self.frame / bar_total) * bar_w)
        pygame.draw.rect(screen, (60, 60, 80), (bar_x, bar_y, bar_w, 18), border_radius=4)
        pygame.draw.rect(screen, ACCENT, (bar_x, bar_y, filled, 18), border_radius=4)
        label = font_sm.render(
            f"{'▶' if self.playing else '⏸'}  {int(self.frame)+1}/{self.total_frames}  ×{self.speed:.1f}",
            True, TEXT_COL
        )
        screen.blit(label, (bar_x + bar_w + 8, h - bar_h + 8))

    def run(self):
        pygame.init()
        w = max(self.anim.width, 300)
        h = max(self.anim.height, 300) + 36
        screen = pygame.display.set_mode((w, h), pygame.RESIZABLE)
        pygame.display.set_caption("PuniFlash Viewer")
        self._precache_all()
        clock = pygame.time.Clock()

        try:
            font_sm = pygame.font.SysFont("consolas", 13)
        except Exception:
            font_sm = pygame.font.Font(None, 14)

        fps = max(1, self.anim.fps)
        frame_acc = 0.0

        running = True
        while running:
            dt = clock.tick(60) / 1000.0

            for event in pygame.event.get():
                if event.type == pgl.QUIT:
                    running = False
                elif event.type == pgl.KEYDOWN:
                    if event.key == pgl.K_SPACE:
                        self.playing = not self.playing
                    elif event.key == pgl.K_RIGHT:
                        self.frame = (int(self.frame) + 1) % self.total_frames
                    elif event.key == pgl.K_LEFT:
                        self.frame = (int(self.frame) - 1) % self.total_frames
                    elif event.key == pgl.K_UP:
                        self.speed = min(4.0, self.speed + 0.25)
                    elif event.key == pgl.K_DOWN:
                        self.speed = max(0.25, self.speed - 0.25)
                    elif event.key == pgl.K_r:
                        self.frame = 0

            if self.playing:
                frame_acc += dt * fps * self.speed
                if frame_acc >= 1.0:
                    steps = int(frame_acc)
                    self.frame = (int(self.frame) + steps) % self.total_frames
                    frame_acc -= steps

            screen.fill(BG_COLOR)
            sorted_layers = list(enumerate(self.anim.layers))
            sorted_layers.reverse()
            
            for idx, layer in sorted_layers:
                group_layers = [l for l in self.anim.layers if l.groupId == layer.groupId]
                self._render_layer(screen, idx, layer, group_layers)

            self.draw_hud(screen, font_sm)
            pygame.display.flip()

        pygame.quit()

# ─────────────────────────────────────────────
#  TKINTER CONTROL PANEL
# ─────────────────────────────────────────────

PANEL_BG   = "#1a1a2e"
PANEL_FG   = "#e0e0f0"
CHECK_BG   = "#16213e"
ACCENT_TK  = "#64c8ff"
BTN_BG     = "#0f3460"
BTN_HOVER  = "#1a5276"

class ControlPanel:
    def __init__(self, renderer: Renderer):
        self.renderer = renderer
        self.root = tk.Tk()
        self.root.title("Layer Control")
        self.root.configure(bg=PANEL_BG)
        self.root.resizable(True, True)
        self.root.geometry("320x480")

        self._vars: dict[int, tk.BooleanVar] = {}
        self._build_ui()

    def _build_ui(self):
        r = self.root

        # ── Title bar ──
        header = tk.Frame(r, bg=PANEL_BG, pady=10)
        header.pack(fill="x", padx=12)
        tk.Label(
            header, text="⚙  LAYER CONTROL",
            font=("Courier New", 12, "bold"),
            bg=PANEL_BG, fg=ACCENT_TK
        ).pack(side="left")

        # ── Info ──
        anim = self.renderer.anim
        info_text = (
            f"v{anim.version}  •  {anim.fps} fps  •  "
            f"{anim.width}×{anim.height}  •  "
            f"{self.renderer.total_frames} frames"
        )
        tk.Label(
            r, text=info_text,
            font=("Courier New", 9), bg=PANEL_BG, fg="#8888aa"
        ).pack(padx=12, anchor="w")

        tk.Frame(r, bg=ACCENT_TK, height=1).pack(fill="x", padx=12, pady=6)

        # ── Bulk buttons ──
        btn_row = tk.Frame(r, bg=PANEL_BG)
        btn_row.pack(fill="x", padx=12, pady=(0, 8))
        self._btn(btn_row, "Show All",  self._show_all).pack(side="left", padx=(0, 6))
        self._btn(btn_row, "Hide All",  self._hide_all).pack(side="left")

        # ── Scrollable layer list ──
        container = tk.Frame(r, bg=PANEL_BG)
        container.pack(fill="both", expand=True, padx=12, pady=4)

        canvas = tk.Canvas(container, bg=PANEL_BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        self.scroll_frame = tk.Frame(canvas, bg=PANEL_BG)

        self.scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Mousewheel binding
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        # Populate layers
        layers = anim.layers if hasattr(anim, 'layers') else []
        sorted_layers = list(enumerate(layers))

        for idx, layer in sorted_layers:
            self._add_layer_row(idx, layer)

        # ── Playback controls ──
        tk.Frame(r, bg=ACCENT_TK, height=1).pack(fill="x", padx=12, pady=6)
        ctrl_row = tk.Frame(r, bg=PANEL_BG)
        ctrl_row.pack(fill="x", padx=12, pady=(0, 10))

        self._btn(ctrl_row, "⏮", lambda: setattr(self.renderer, 'frame', 0)).pack(side="left", padx=2)
        self._btn(ctrl_row, "⏪", lambda: self._step(-1)).pack(side="left", padx=2)
        self._btn(ctrl_row, "▶/⏸", lambda: setattr(self.renderer, 'playing', not self.renderer.playing)).pack(side="left", padx=2)
        self._btn(ctrl_row, "⏩", lambda: self._step(1)).pack(side="left", padx=2)

        # Speed slider
        spd_frame = tk.Frame(r, bg=PANEL_BG)
        spd_frame.pack(fill="x", padx=12, pady=(0, 12))
        tk.Label(spd_frame, text="Speed", font=("Courier New", 9),
                 bg=PANEL_BG, fg=PANEL_FG).pack(side="left")
        self.speed_var = tk.DoubleVar(value=1.0)
        spd_slider = tk.Scale(
            spd_frame, from_=0.25, to=4.0, resolution=0.25,
            orient="horizontal", variable=self.speed_var,
            command=lambda v: setattr(self.renderer, 'speed', float(v)),
            bg=PANEL_BG, fg=PANEL_FG, troughcolor=CHECK_BG,
            highlightthickness=0, activebackground=ACCENT_TK,
            sliderrelief="flat", length=180
        )
        spd_slider.pack(side="left", padx=8)

    def _btn(self, parent, text, cmd):
        b = tk.Button(
            parent, text=text, command=cmd,
            font=("Courier New", 9, "bold"),
            bg=BTN_BG, fg=PANEL_FG,
            activebackground=BTN_HOVER, activeforeground=PANEL_FG,
            relief="flat", padx=8, pady=4, cursor="hand2"
        )
        b.bind("<Enter>", lambda e: b.config(bg=BTN_HOVER))
        b.bind("<Leave>", lambda e: b.config(bg=BTN_BG))
        return b

    def _add_layer_row(self, idx: int, layer):
        var = tk.BooleanVar(value=True)
        self._vars[idx] = var

        row = tk.Frame(self.scroll_frame, bg=CHECK_BG, pady=5, padx=8)
        row.pack(fill="x", pady=2)

        # Visibility checkbox
        chk = tk.Checkbutton(
            row, variable=var,
            command=lambda i=idx, v=var: self.renderer.toggle_layer(i, v.get()),
            bg=CHECK_BG, activebackground=CHECK_BG,
            selectcolor=BTN_BG, fg=ACCENT_TK,
            relief="flat", cursor="hand2"
        )
        chk.pack(side="left")

        # Layer info
        name = layer.layerName or f"Layer {idx}"
        tex  = os.path.basename(layer.texturePath) if layer.texturePath else "—"
        info_frame = tk.Frame(row, bg=CHECK_BG)
        info_frame.pack(side="left", fill="x", expand=True)

        tk.Label(
            info_frame, text=f"[G:{layer.groupId:03d}]  {name}",
            font=("Courier New", 10, "bold"),
            bg=CHECK_BG, fg=PANEL_FG, anchor="w"
        ).pack(fill="x")

        tk.Label(
            info_frame, text=f"  {tex}  •  {layer.frameCount} frames",
            font=("Courier New", 8),
            bg=CHECK_BG, fg="#666688", anchor="w"
        ).pack(fill="x")

        # Solo button
        solo_btn = self._btn(row, "S", lambda i=idx: self._solo(i))
        solo_btn.pack(side="right", padx=(4, 0))

    def _show_all(self):
        for i, var in self._vars.items():
            var.set(True)
            self.renderer.toggle_layer(i, True)

    def _hide_all(self):
        for i, var in self._vars.items():
            var.set(False)
            self.renderer.toggle_layer(i, False)

    def _solo(self, solo_idx: int):
        for i, var in self._vars.items():
            vis = (i == solo_idx)
            var.set(vis)
            self.renderer.toggle_layer(i, vis)

    def _step(self, delta: int):
        self.renderer.frame = (int(self.renderer.frame) + delta) % max(1, self.renderer.total_frames)

    def run(self):
        self.root.mainloop()


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

def pick_file_tk() -> str | None:
    root = tk.Tk()
    root.withdraw()
    path = filedialog.askopenfilename(
        title="Open PuniFlash animation",
        filetypes=[("PuniFlash files", "*.pfi *.bin *.dat *"), ("All files", "*.*")]
    )
    root.destroy()
    return path or None
    
def main():
    #example files, index 0 is .dat file, index 1 is folder where are stored textures
    lst = [
        [r"C:\Users\celestin\Downloads\bypasser_v1\assets\image\start\start_ef_resourcedl01_loop.dat", r"C:\Users\celestin\Downloads\bypasser_v1\assets\image\folder"],
        [r"C:\Users\celestin\Downloads\bypasser_v1\assets\image\start\start_ef_resourcedl01_loop.dat", r"C:\Users\celestin\Downloads\bypasser_v1\assets\image\folder"],
        [r"C:\Users\celestin\Downloads\bypasser_v1\assets\image\start\start_ef_resourcedl01_end.dat", r"C:\Users\celestin\Downloads\bypasser_v1\assets\image\folder"],
        [r"C:\Users\celestin\Downloads\bypasser_v1\assets\image\title\title_ef_title_loop01.dat", r"C:\Users\celestin\Downloads\bypasser_v1\assets\image\folder"],
        [r"C:\Users\celestin\Desktop\puniemu-master\Tools\new_puni_dump\skill\skill_91_1_9002651.dat", r"C:\Users\celestin\Desktop\puniemu-master\Tools\new_puni_dump"],
        [r"C:\Users\celestin\Desktop\puniemu-master\Tools\new_puni_dump\skill\skill_91_1_9002510.dat", r"C:\Users\celestin\Desktop\puniemu-master\Tools\new_puni_dump"],
        [r"C:\Users\celestin\Desktop\puniemu-master\Tools\new_puni_dump\skill\skill_94_1_9001398.dat", r"C:\Users\celestin\Desktop\puniemu-master\Tools\new_puni_dump"],
        [r"C:\Users\celestin\Desktop\puniemu-master\Tools\new_puni_dump\image\title\title_ef_title19_loop01.dat", r"C:\Users\celestin\Desktop\puniemu-master\Tools\new_puni_dump"],
        [r"C:\Users\celestin\Desktop\puniemu-master\Tools\new_puni_dump\image\union\union_ui_main01.dat", r"C:\Users\celestin\Desktop\puniemu-master\Tools\new_puni_dump"]
    ]
    example_index = 5
    filepath = lst[example_index][0]
    # Parse animation
    try:
        anim = PuniFlashImage().open(filepath)
    except Exception as e:
        messagebox.showerror("Parse error", str(e))
        return

    asset_dir = os.path.dirname(os.path.abspath(filepath))
    renderer = Renderer(anim, lst[example_index][1], filepath)

    # Run pygame in a separate thread
    pygame_thread = threading.Thread(target=renderer.run, daemon=True)
    pygame_thread.start()

    # Run Tkinter control panel in main thread
    panel = ControlPanel(renderer)
    panel.run()

    # When tkinter window closes, also quit
    pygame.quit()


if __name__ == "__main__":
    main()