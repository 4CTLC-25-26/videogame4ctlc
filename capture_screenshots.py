from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "screenshot"
CONFIG_PATH = ROOT / "auracade.json"
LAUNCHER_PATH = ROOT / "launcher.py"


SITE_HOOK = r'''
import atexit
import os
import time

try:
    import pygame
except Exception:
    pygame = None

_OUTPUT = os.environ.get("AURACADE_CAPTURE_OUTPUT", "").strip()
_CAPTURE_AFTER_MS = int(os.environ.get("AURACADE_CAPTURE_AFTER_MS", "10000"))
_QUIT_AFTER_MS = int(os.environ.get("AURACADE_QUIT_AFTER_MS", "22000"))

_captured = False
_start = time.time()


def _elapsed_ms() -> int:
    return int((time.time() - _start) * 1000)


def _save_if_possible() -> None:
    global _captured
    if _captured or not _OUTPUT or pygame is None:
        return
    if not pygame.get_init() or not pygame.display.get_init():
        return
    surface = pygame.display.get_surface()
    if surface is None:
        return
    try:
        os.makedirs(os.path.dirname(_OUTPUT), exist_ok=True)
        pygame.image.save(surface, _OUTPUT)
        _captured = True
    except Exception:
        pass


def _request_quit() -> None:
    if pygame is None or not pygame.get_init():
        return
    try:
        pygame.event.post(pygame.event.Event(pygame.QUIT))
    except Exception:
        pass


def _on_frame() -> None:
    if _elapsed_ms() >= _CAPTURE_AFTER_MS:
        _save_if_possible()
    if _elapsed_ms() >= _QUIT_AFTER_MS:
        _save_if_possible()
        _request_quit()


if pygame is not None:
    try:
        _orig_flip = pygame.display.flip

        def _patched_flip(*args, **kwargs):
            result = _orig_flip(*args, **kwargs)
            _on_frame()
            return result

        pygame.display.flip = _patched_flip
    except Exception:
        pass

    try:
        _orig_update = pygame.display.update

        def _patched_update(*args, **kwargs):
            result = _orig_update(*args, **kwargs)
            _on_frame()
            return result

        pygame.display.update = _patched_update
    except Exception:
        pass

atexit.register(_save_if_possible)
'''


@dataclass(frozen=True)
class CaptureTarget:
    slug: str
    title: str
    script: Path


def load_targets() -> list[CaptureTarget]:
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    projects = data.get("projects", [])
    targets: list[CaptureTarget] = []
    for proj in projects:
        slug = str(proj.get("id", "game"))
        title = str(proj.get("display_name", slug))
        rel = str(proj["file"])
        script = ROOT / rel
        targets.append(CaptureTarget(slug=slug, title=title, script=script))
    return targets


def build_env(hook_dir: Path, output_file: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(hook_dir) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["AURACADE_CAPTURE_OUTPUT"] = str(output_file)
    env["AURACADE_CAPTURE_AFTER_MS"] = "10000"
    env["AURACADE_QUIT_AFTER_MS"] = "22000"
    return env


def run_capture(command: list[str], cwd: Path, hook_dir: Path, output_file: Path, timeout_sec: int) -> bool:
    env = build_env(hook_dir, output_file)
    process = None
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        process.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        if process is not None:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                process.kill()
    except Exception:
        if process is not None:
            try:
                process.kill()
            except Exception:
                pass
    return output_file.exists() and output_file.stat().st_size > 0


def build_reconstructed_image(target: CaptureTarget, output_file: Path) -> bool:
    try:
        import pygame

        pygame.init()
        pygame.font.init()

        inferred = infer_game_resolution(target.script)
        if inferred is None:
            width, height = 1366, 768
        else:
            width, height = inferred
        surface = pygame.Surface((width, height))

        # Simple CRT-like gradient background.
        for y in range(height):
            c = 14 + int((y / max(1, height - 1)) * 32)
            pygame.draw.line(surface, (c, c + 8, c + 20), (0, y), (width, y))

        panel = pygame.Rect(120, 100, width - 240, height - 200)
        pygame.draw.rect(surface, (20, 30, 48), panel, border_radius=20)
        pygame.draw.rect(surface, (70, 120, 190), panel, width=3, border_radius=20)

        title_font = pygame.font.SysFont("consolas", 54, bold=True)
        body_font = pygame.font.SysFont("consolas", 28)

        y = 180
        for line in wrap_lines(title_font, target.title, panel.width - 80):
            text = title_font.render(line, True, (236, 244, 255))
            surface.blit(text, (panel.x + 40, y))
            y += 64

        y += 20
        desc = body_font.render("Schermata ricostruita dal codice", True, (168, 209, 242))
        surface.blit(desc, (panel.x + 40, y))
        y += 44

        slug = body_font.render(f"id: {target.slug}", True, (255, 190, 84))
        surface.blit(slug, (panel.x + 40, y))

        output_file.parent.mkdir(parents=True, exist_ok=True)
        pygame.image.save(surface, str(output_file))
        pygame.quit()
        return True
    except Exception:
        return False


def wrap_lines(font, text: str, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur: list[str] = []
    for w in words:
        probe = " ".join(cur + [w])
        if font.size(probe)[0] <= max_width:
            cur.append(w)
        else:
            if cur:
                lines.append(" ".join(cur))
            cur = [w]
    if cur:
        lines.append(" ".join(cur))
    return lines


def _extract_int_constants(text: str) -> dict[str, int]:
    constants: dict[str, int] = {}

    for match in re.finditer(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\d+)\s*$", text, flags=re.MULTILINE):
        constants[match.group(1)] = int(match.group(2))

    for match in re.finditer(
        r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\d+)\s*,\s*(\d+)\s*$",
        text,
        flags=re.MULTILINE,
    ):
        constants[match.group(1)] = int(match.group(3))
        constants[match.group(2)] = int(match.group(4))

    return constants


def infer_game_resolution(script: Path) -> tuple[int, int] | None:
    """Infer the game window resolution from script source, if possible."""
    try:
        text = script.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    constants = _extract_int_constants(text)

    # Direct numeric set_mode, e.g. set_mode((800, 600)).
    direct = re.search(r"set_mode\s*\(\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", text)
    if direct:
        return int(direct.group(1)), int(direct.group(2))

    # Symbolic set_mode, e.g. set_mode((WIDTH, HEIGHT)).
    symbolic = re.search(
        r"set_mode\s*\(\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)",
        text,
    )
    if symbolic:
        w_name = symbolic.group(1)
        h_name = symbolic.group(2)
        if w_name in constants and h_name in constants:
            return constants[w_name], constants[h_name]

    return None


def existing_capture_for_slug(slug: str) -> Path | None:
    """Return an existing screenshot path for a slug, if present and non-empty."""
    candidates = [
        OUTPUT_DIR / f"{slug}.png",
        OUTPUT_DIR / f"{slug}_ricostruito.png",
    ]
    for path in candidates:
        if path.exists() and path.stat().st_size > 0:
            return path
    return None


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    targets = load_targets()

    python = Path(sys.executable)
    results: list[dict[str, str]] = []

    with tempfile.TemporaryDirectory(prefix="auracade4ctlc_capture_") as tmp:
        hook_dir = Path(tmp)
        (hook_dir / "sitecustomize.py").write_text(SITE_HOOK, encoding="utf-8")

        launcher_out = OUTPUT_DIR / "auracade_launcher.png"
        launcher_existing = existing_capture_for_slug("auracade_launcher")
        if launcher_existing is not None:
            print("Skip launcher: screenshot gia' presente", flush=True)
            results.append(
                {
                    "slug": "auracade_launcher",
                    "title": "AURACADE 199X Launcher",
                    "status": "skipped_existing",
                    "file": launcher_existing.name,
                }
            )
        else:
            print("Catturo: launcher", flush=True)
            launcher_ok = run_capture([str(python), str(LAUNCHER_PATH)], ROOT, hook_dir, launcher_out, timeout_sec=32)
            results.append({
                "slug": "auracade_launcher",
                "title": "AURACADE 199X Launcher",
                "status": "real" if launcher_ok else "failed",
                "file": launcher_out.name if launcher_ok else "",
            })

        for target in targets:
            existing = existing_capture_for_slug(target.slug)
            if existing is not None:
                print(f"Skip: {target.slug} (gia' presente)", flush=True)
                results.append(
                    {
                        "slug": target.slug,
                        "title": target.title,
                        "status": "skipped_existing",
                        "file": existing.name,
                    }
                )
                continue

            real_out = OUTPUT_DIR / f"{target.slug}.png"
            print(f"Catturo: {target.slug}", flush=True)
            ok = run_capture([str(python), str(target.script)], target.script.parent, hook_dir, real_out, timeout_sec=32)
            status = "real"
            final_file = real_out
            if not ok:
                recon_out = OUTPUT_DIR / f"{target.slug}_ricostruito.png"
                rec_ok = build_reconstructed_image(target, recon_out)
                status = "reconstructed" if rec_ok else "failed"
                final_file = recon_out if rec_ok else real_out
            results.append(
                {
                    "slug": target.slug,
                    "title": target.title,
                    "status": status,
                    "file": final_file.name if final_file.exists() else "",
                }
            )

    report = [
        "# Report screenshot",
        "",
        "Legenda: real = screenshot vero in esecuzione, reconstructed = schermata ricostruita da codice, skipped_existing = screenshot gia' presente.",
        "",
    ]
    for item in results:
        report.append(f"- {item['slug']}: {item['status']} -> {item['file']}")

    (OUTPUT_DIR / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    real_count = sum(1 for item in results if item["status"] == "real")
    recon_count = sum(1 for item in results if item["status"] == "reconstructed")
    failed_count = sum(1 for item in results if item["status"] == "failed")
    skipped_count = sum(1 for item in results if item["status"] == "skipped_existing")

    print(f"Screenshot reali: {real_count}")
    print(f"Ricostruiti: {recon_count}")
    print(f"Gia' presenti (saltati): {skipped_count}")
    print(f"Falliti: {failed_count}")
    print(f"Output: {OUTPUT_DIR}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
