import json, math, os, socket, struct, sys, threading, time
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import tkinter as tk
from tkinter import ttk, messagebox
from Crypto.Cipher import Salsa20

APP = "Speed Dynamics GT7"
SEND_PORT = 33739
RECV_PORT = 33740
WEB_PORT = 8080
KEY = b"Simulator Interface Packet GT7 ver. 0.0"[:32]
MAGIC = 0x47375330

# Live delta settings
SAMPLE_INTERVAL = 0.05       # reference/current track samples: ~20 Hz
SEARCH_BACK = 25             # allow a little backwards correction
SEARCH_FORWARD = 250         # tolerate faster/slower driving and braking differences
MIN_REFERENCE_LAP_MS = 5000  # ignore invalid/very short laps

state = {
    "connected": False, "mode": "", "packets": 0, "valid": 0,
    "bytes": 0, "packet_size": 0, "speed": 0.0, "rpm": 0,
    "gear": 0, "throttle": 0, "brake": 0, "fuel": 0.0,
    "fuel_capacity": 0.0, "lap": 0, "total_laps": 0,
    "best_lap_ms": -1, "last_lap_ms": -1, "current_lap_ms": -1,
    "position": 0, "cars": 0, "delta_ms": None,
    "reference_ms": None, "reference_ready": False,
    "reference_samples": 0,
}
lock = threading.Lock()


def base_path():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def decrypt_packet(packet, mode):
    if len(packet) not in (296, 368):
        return None

    iv1 = int.from_bytes(packet[0x40:0x44], "little")
    constants = (
        [0xDEADBEEF, 0x55FABB4F, 0xDEADBEAF]
        if mode == "C"
        else [0xDEADBEAF]
    )

    for constant in constants:
        iv2 = iv1 ^ constant
        nonce = iv2.to_bytes(4, "little") + iv1.to_bytes(4, "little")
        try:
            plain = Salsa20.new(key=KEY, nonce=nonce).decrypt(packet)
            if int.from_bytes(plain[:4], "little") == MAGIC:
                return plain
        except Exception:
            pass
    return None


def f32(data, offset):
    return struct.unpack_from("<f", data, offset)[0]


def i16(data, offset):
    return struct.unpack_from("<h", data, offset)[0]


def i32(data, offset):
    return struct.unpack_from("<i", data, offset)[0]


def parse_common(data):
    x, y, z = struct.unpack_from("<fff", data, 0x04)
    return {
        "speed": max(0.0, f32(data, 0x4C) * 3.6),
        "rpm": max(0, round(f32(data, 0x3C))),
        "fuel": max(0.0, f32(data, 0x44)),
        "fuel_capacity": max(0.0, f32(data, 0x48)),
        "lap": max(0, i16(data, 0x74)),
        "total_laps": max(0, i16(data, 0x76)),
        "best_lap_ms": i32(data, 0x78),
        "last_lap_ms": i32(data, 0x7C),
        "position": max(0, i16(data, 0x84)),
        "cars": max(0, i16(data, 0x86)),
        "gear": data[0x90] & 0x0F,
        "throttle": round(data[0x91] / 255 * 100),
        "brake": round(data[0x92] / 255 * 100),
        "x": x,
        "y": y,
        "z": z,
    }


def parse_packet(plain):
    p = parse_common(plain[:296])
    if len(plain) >= 368:
        p["current_lap_ms"] = i32(plain, 0x140)
        p["packet"] = "C"
    else:
        p["current_lap_ms"] = -1
        p["packet"] = "A"
    return p


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api"):
            with lock:
                payload = json.dumps(state).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            super().do_GET()

    def log_message(self, *args):
        pass


def start_web():
    os.chdir(base_path() / "web")
    ThreadingHTTPServer(("0.0.0.0", WEB_PORT), Handler).serve_forever()


class Bridge:
    def __init__(self, ip, status):
        self.ip = ip
        self.status = status
        self.running = False
        self.mode = "C"

        # Reference lap = the fastest complete lap recorded by this app session.
        self.reference_samples = []
        self.reference_lap_ms = None
        self.reference_cursor = 0

        # Current lap samples.
        self.current_samples = []
        self.last_sample_time = 0.0
        self.last_lap = None
        self.lap_start_monotonic = None
        self.packet_counter = 0

    def start(self):
        self.running = True
        threading.Thread(target=self.run, daemon=True).start()

    def stop(self):
        self.running = False

    @staticmethod
    def distance_sq(a, b):
        dx = a[0] - b[0]
        dy = a[1] - b[1]
        dz = a[2] - b[2]
        return dx * dx + dy * dy + dz * dz

    def choose_reference_point(self, position):
        if not self.reference_samples:
            return None

        count = len(self.reference_samples)

        # On the first point of a new lap, search the start area.
        if self.reference_cursor <= 2:
            lo = 0
            hi = min(count, 120)
        else:
            lo = max(0, self.reference_cursor - SEARCH_BACK)
            hi = min(count, self.reference_cursor + SEARCH_FORWARD + 1)

        best_idx = lo
        best_dist = float("inf")

        for idx in range(lo, hi):
            sample = self.reference_samples[idx]
            d = self.distance_sq(position, (sample[1], sample[2], sample[3]))
            if d < best_dist:
                best_dist = d
                best_idx = idx

        self.reference_cursor = best_idx
        return self.reference_samples[best_idx]

    def finish_current_lap(self, completed_ms):
        if not self.current_samples or completed_ms < MIN_REFERENCE_LAP_MS:
            return

        # Only a complete recorded lap can become the spatial reference.
        if (
            self.reference_lap_ms is None
            or completed_ms < self.reference_lap_ms
        ):
            self.reference_lap_ms = int(completed_ms)
            self.reference_samples = list(self.current_samples)
            self.reference_cursor = 0

    def reset_current_lap(self, now):
        self.current_samples = []
        self.last_sample_time = 0.0
        self.reference_cursor = 0
        self.lap_start_monotonic = now

    def calculate_live_delta(self, p):
        if not self.reference_samples:
            return None

        current_ms = p["current_lap_ms"]
        if current_ms is None or current_ms < 0:
            return None

        reference = self.choose_reference_point((p["x"], p["y"], p["z"]))
        if reference is None:
            return None

        # reference = (lap_ms, x, y, z)
        return int(current_ms - reference[0])

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", RECV_PORT))
            sock.settimeout(0.5)
        except Exception as exc:
            self.status("UDP 33740 FEHLER: " + str(exc))
            return

        self.status("UDP 33740 bereit · Packet C wird angefordert")
        started = time.monotonic()
        last_request = 0.0

        while self.running:
            now = time.monotonic()

            if now - last_request >= 1.0:
                try:
                    sock.sendto(self.mode.encode("ascii"), (self.ip, SEND_PORT))
                except Exception as exc:
                    self.status("UDP-Sende-Fehler: " + str(exc))
                last_request = now

            try:
                raw, _ = sock.recvfrom(4096)

                with lock:
                    state["packets"] += 1
                    state["bytes"] += len(raw)
                    state["packet_size"] = len(raw)

                plain = decrypt_packet(raw, self.mode)

                # Keep the proven fallback if Packet C cannot be decoded.
                if (
                    plain is None
                    and self.mode == "C"
                    and now - started > 4
                ):
                    self.mode = "A"
                    self.status(
                        "Packet C nicht erkannt · V0.5 Packet A wird verwendet"
                    )
                    continue

                if plain is None:
                    continue

                p = parse_packet(plain)
                self.packet_counter += 1

                # Packet A has no live current-lap field, so use the local
                # elapsed time exactly as the previous working version did.
                if p["packet"] == "A":
                    self.mode = "A"

                    if self.last_lap is None:
                        self.last_lap = p["lap"]
                        self.reset_current_lap(now)

                    elif p["lap"] != self.last_lap:
                        completed = p["last_lap_ms"]
                        if completed <= 0 and self.lap_start_monotonic is not None:
                            completed = int(
                                (now - self.lap_start_monotonic) * 1000
                            )

                        self.finish_current_lap(completed)
                        self.last_lap = p["lap"]
                        self.reset_current_lap(now)

                    if self.lap_start_monotonic is None:
                        self.lap_start_monotonic = now

                    p["current_lap_ms"] = int(
                        (now - self.lap_start_monotonic) * 1000
                    )

                else:
                    # Packet C: use GT7's current lap clock. On a lap change,
                    # last_lap_ms is the completed lap and current_lap_ms has
                    # already reset for the new lap.
                    if self.last_lap is None:
                        self.last_lap = p["lap"]
                        self.reset_current_lap(now)

                    elif p["lap"] != self.last_lap:
                        completed = p["last_lap_ms"]
                        self.finish_current_lap(completed)
                        self.last_lap = p["lap"]
                        self.reset_current_lap(now)

                # Record current trajectory at ~20 Hz.
                if (
                    self.lap_start_monotonic is not None
                    and now - self.last_sample_time >= SAMPLE_INTERVAL
                    and p["current_lap_ms"] >= 0
                ):
                    self.current_samples.append(
                        (
                            int(p["current_lap_ms"]),
                            float(p["x"]),
                            float(p["y"]),
                            float(p["z"]),
                        )
                    )
                    self.last_sample_time = now

                delta = self.calculate_live_delta(p)

                with lock:
                    state.update(p)
                    state["connected"] = True
                    state["mode"] = p["packet"]
                    state["valid"] += 1
                    state["delta_ms"] = delta
                    state["reference_ms"] = self.reference_lap_ms
                    state["reference_ready"] = bool(self.reference_samples)
                    state["reference_samples"] = len(self.reference_samples)
                    valid = state["valid"]

                self.status(
                    f"GT7 LIVE VERBUNDEN · Packet {p['packet']} · "
                    f"{len(raw)} Bytes · gültige Pakete: {valid}"
                )

            except socket.timeout:
                pass
            except Exception as exc:
                self.status("UDP-Fehler: " + str(exc))

        sock.close()


class App:
    def __init__(self):
        self.bridge = None
        self.root = tk.Tk()
        self.root.title(APP)
        self.root.geometry("650x460")
        self.root.configure(bg="#111111")

        box = tk.Frame(self.root, bg="#111111")
        box.pack(fill="both", expand=True, padx=32, pady=25)

        tk.Label(
            box,
            text="SPEED DYNAMICS",
            font=("Segoe UI", 26, "bold"),
            fg="#d7b45a",
            bg="#111111",
        ).pack(anchor="w")

        tk.Label(
            box,
            text="GT7 Race Engineer · Complete Edition",
            font=("Segoe UI", 11),
            fg="#aaaaaa",
            bg="#111111",
        ).pack(anchor="w", pady=(0, 22))

        tk.Label(
            box,
            text="IP-Adresse deiner PS5",
            font=("Segoe UI", 10, "bold"),
            fg="white",
            bg="#111111",
        ).pack(anchor="w")

        self.ip = tk.StringVar(value="192.168.178.200")
        ttk.Entry(box, textvariable=self.ip, font=("Segoe UI", 14)).pack(
            fill="x", pady=(6, 12)
        )

        tk.Button(
            box,
            text="MIT GT7 VERBINDEN",
            command=self.connect,
            font=("Segoe UI", 12, "bold"),
            bg="#d7b45a",
            fg="#111111",
            relief="flat",
            pady=10,
        ).pack(fill="x")

        self.msg = tk.StringVar(value="Bereit")
        tk.Label(
            box,
            textvariable=self.msg,
            font=("Segoe UI", 11, "bold"),
            fg="#dddddd",
            bg="#111111",
            wraplength=580,
            justify="left",
        ).pack(anchor="w", pady=(16, 8))

        self.diag = tk.StringVar(
            value="Empfangen: 0    Gültig: 0    Bytes: 0    Paket: 0"
        )
        tk.Label(
            box,
            textvariable=self.diag,
            font=("Consolas", 9),
            fg="#aaaaaa",
            bg="#111111",
        ).pack(anchor="w", pady=(2, 14))

        self.url = f"http://{lan_ip()}:{WEB_PORT}"
        tk.Label(
            box,
            text="iPhone – im selben WLAN in Safari öffnen:",
            font=("Segoe UI", 9),
            fg="#888888",
            bg="#111111",
        ).pack(anchor="w")

        tk.Label(
            box,
            text=self.url,
            font=("Consolas", 14, "bold"),
            fg="#d7b45a",
            bg="#111111",
        ).pack(anchor="w", pady=(3, 7))

        tk.Button(
            box,
            text="ADRESSE KOPIEREN",
            command=self.copy_url,
            bg="#282828",
            fg="white",
            relief="flat",
            pady=7,
        ).pack(fill="x")

        threading.Thread(target=start_web, daemon=True).start()

    def status(self, text):
        with lock:
            diagnostics = (
                f"Empfangen: {state['packets']}    "
                f"Gültig: {state['valid']}    "
                f"Bytes: {state['bytes']}    "
                f"Paket: {state['packet_size']}"
            )

        self.root.after(
            0, lambda: (self.msg.set(text), self.diag.set(diagnostics))
        )

    def connect(self):
        ip = self.ip.get().strip()
        try:
            socket.inet_aton(ip)
        except OSError:
            messagebox.showerror(
                "PS5-IP", "Bitte eine gültige PS5-IP eingeben."
            )
            return

        if self.bridge:
            self.bridge.stop()

        with lock:
            state.update(
                {
                    "connected": False,
                    "mode": "",
                    "packets": 0,
                    "valid": 0,
                    "bytes": 0,
                    "packet_size": 0,
                    "delta_ms": None,
                    "reference_ms": None,
                    "reference_ready": False,
                    "reference_samples": 0,
                }
            )

        self.bridge = Bridge(ip, self.status)
        self.bridge.start()

    def copy_url(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.url)
        self.msg.set("iPhone-Adresse kopiert")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
