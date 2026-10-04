import json, os, socket, struct, sys, threading, time, math
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

state = {
    "connected": False, "mode": "", "packets": 0, "valid": 0,
    "bytes": 0, "packet_size": 0, "speed": 0.0, "rpm": 0,
    "gear": 0, "throttle": 0, "brake": 0, "fuel": 0.0,
    "fuel_capacity": 0.0, "lap": 0, "total_laps": 0,
    "best_lap_ms": -1, "last_lap_ms": -1, "current_lap_ms": -1,
    "position": 0, "cars": 0, "delta_ms": None,
    "reference_ms": None, "reference_ready": False,
    "reference_source": "", "reference_samples": 0,
    "pos_x": 0.0, "pos_y": 0.0, "pos_z": 0.0
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
    constants = [0xDEADBEEF, 0x55FABB4F, 0xDEADBEAF] if mode == "C" else [0xDEADBEAF]
    for c in constants:
        iv2 = iv1 ^ c
        nonce = iv2.to_bytes(4, "little") + iv1.to_bytes(4, "little")
        try:
            plain = Salsa20.new(key=KEY, nonce=nonce).decrypt(packet)
            if int.from_bytes(plain[:4], "little") == MAGIC:
                return plain
        except Exception:
            pass
    return None

def f32(d, o):
    return struct.unpack_from("<f", d, o)[0]

def i16(d, o):
    return struct.unpack_from("<h", d, o)[0]

def i32(d, o):
    return struct.unpack_from("<i", d, o)[0]

def parse_common(d):
    return {
        # GT7 Packet A/C world position. X/Y/Z are at 0x04/0x08/0x0C.
        "pos_x": f32(d, 0x04),
        "pos_y": f32(d, 0x08),
        "pos_z": f32(d, 0x0C),
        "speed": max(0.0, f32(d, 0x4C) * 3.6),
        "rpm": max(0, round(f32(d, 0x3C))),
        "fuel": max(0.0, f32(d, 0x44)),
        "fuel_capacity": max(0.0, f32(d, 0x48)),
        "lap": max(0, i16(d, 0x74)),
        "total_laps": max(0, i16(d, 0x76)),
        "best_lap_ms": i32(d, 0x78),
        "last_lap_ms": i32(d, 0x7C),
        "position": max(0, i16(d, 0x84)),
        "cars": max(0, i16(d, 0x86)),
        "gear": d[0x90] & 0x0F,
        "throttle": round(d[0x91] / 255 * 100),
        "brake": round(d[0x92] / 255 * 100),
    }

def parse_packet(plain):
    p = parse_common(plain[:296])
    if len(plain) >= 368:
        # Packet C: exact live current-lap timer.
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
                payload = json.dumps(state).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
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
    """
    Live delta implementation:
    - GT7 Packet C supplies the exact current lap timer and world position.
    - A completed lap is recorded as (world X/Z position, elapsed lap time).
    - The fastest completed recorded lap becomes the reference trace.
    - During the next lap, the current X/Z position is matched to the
      corresponding nearby point on that reference trace.
    - Delta = current elapsed time - reference elapsed time at the same track
      position. It therefore does NOT simply count down to zero at the finish.
    """

    def __init__(self, ip, status):
        self.ip = ip
        self.status = status
        self.running = False
        self.mode = "C"

        self.last_lap = None
        self.current_trace = []
        self.reference_trace = []
        self.reference_ms = None
        self.reference_source = ""
        self.last_ref_idx = 0

    def start(self):
        self.running = True
        threading.Thread(target=self.run, daemon=True).start()

    def stop(self):
        self.running = False

    @staticmethod
    def _distance_sq(a, b):
        dx = a[0] - b[0]
        dz = a[1] - b[1]
        return dx * dx + dz * dz

    def _finish_lap(self, completed_ms):
        # Ignore tiny/invalid laps.
        if completed_ms <= 1000 or len(self.current_trace) < 20:
            self.current_trace = []
            return

        # Store the actual completed lap as a spatial time trace.
        candidate = self.current_trace[:]

        # The reference should be the fastest completed lap that we have
        # actually recorded, because a GT7 best-lap number alone contains no
        # historical position/time trace.
        if self.reference_ms is None or completed_ms < self.reference_ms:
            self.reference_ms = int(completed_ms)
            self.reference_trace = candidate
            self.reference_source = "recorded"
            self.last_ref_idx = 0

        self.current_trace = []

    def _live_delta(self, x, z, current_ms):
        if not self.reference_trace or current_ms < 0:
            return None

        n = len(self.reference_trace)
        # Search locally to keep the mapping stable on corners and avoid
        # jumping to a nearby but wrong section of the circuit.
        lo = max(0, self.last_ref_idx - 25)
        hi = min(n, self.last_ref_idx + 180)

        best_i = self.last_ref_idx
        best_d = float("inf")
        for i in range(lo, hi):
            rx, rz, _rt = self.reference_trace[i]
            d = (x - rx) * (x - rx) + (z - rz) * (z - rz)
            if d < best_d:
                best_d = d
                best_i = i

        # If the local window is not close enough, make a wider search.
        # This is mainly useful immediately after a slow/very different lap.
        if best_d > 400.0:
            step = 3
            for i in range(0, n, step):
                rx, rz, _rt = self.reference_trace[i]
                d = (x - rx) * (x - rx) + (z - rz) * (z - rz)
                if d < best_d:
                    best_d = d
                    best_i = i

        self.last_ref_idx = best_i

        # Interpolate between neighboring reference samples for a smoother
        # live value instead of a staircase.
        if best_i <= 0:
            ref_t = self.reference_trace[0][2]
        elif best_i >= n - 1:
            ref_t = self.reference_trace[-1][2]
        else:
            x1, z1, t1 = self.reference_trace[best_i - 1]
            x2, z2, t2 = self.reference_trace[best_i]
            d1 = math.hypot(x - x1, z - z1)
            d2 = math.hypot(x - x2, z - z2)
            if d1 + d2 > 0:
                ref_t = t1 + (t2 - t1) * (d1 / (d1 + d2))
            else:
                ref_t = t2

        return int(round(current_ms - ref_t))

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", RECV_PORT))
            sock.settimeout(0.5)
        except Exception as e:
            self.status("UDP 33740 FEHLER: " + str(e))
            return

        self.status("UDP 33740 bereit · Packet C wird angefordert")
        started = time.monotonic()
        last_request = 0

        while self.running:
            now = time.monotonic()

            if now - last_request >= 1:
                try:
                    sock.sendto(self.mode.encode(), (self.ip, SEND_PORT))
                except Exception as e:
                    self.status("UDP-Sende-Fehler: " + str(e))
                last_request = now

            try:
                raw, _ = sock.recvfrom(4096)

                with lock:
                    state["packets"] += 1
                    state["bytes"] += len(raw)
                    state["packet_size"] = len(raw)

                plain = decrypt_packet(raw, self.mode)

                if plain is None and self.mode == "C" and now - started > 4:
                    self.mode = "A"
                    self.status("Packet C nicht erkannt · Packet A wird verwendet")
                    continue

                if plain is None:
                    continue

                p = parse_packet(plain)

                if p["packet"] == "A":
                    self.mode = "A"
                    # Packet A has no exact live-lap field, so maintain a local
                    # timer only as a fallback.
                    if self.last_lap is None:
                        self.last_lap = p["lap"]
                        self.local_lap_start = now
                    elif p["lap"] != self.last_lap:
                        completed = p["last_lap_ms"]
                        if completed <= 0 and hasattr(self, "local_lap_start"):
                            completed = int((now - self.local_lap_start) * 1000)
                        self._finish_lap(completed)
                        self.last_lap = p["lap"]
                        self.local_lap_start = now

                    if not hasattr(self, "local_lap_start"):
                        self.local_lap_start = now

                    p["current_lap_ms"] = int((now - self.local_lap_start) * 1000)

                else:
                    # Packet C supplies the exact current lap timer.
                    if self.last_lap is None:
                        self.last_lap = p["lap"]
                    elif p["lap"] != self.last_lap:
                        completed = p["last_lap_ms"]
                        self._finish_lap(completed)
                        self.last_lap = p["lap"]

                current_ms = p["current_lap_ms"]

                # Record the current lap continuously.  30 Hz is sufficient
                # for a spatial live delta and keeps traces compact.
                if current_ms >= 0:
                    if not self.current_trace or current_ms - self.current_trace[-1][2] >= 33:
                        self.current_trace.append(
                            (p["pos_x"], p["pos_z"], current_ms)
                        )

                delta = self._live_delta(
                    p["pos_x"], p["pos_z"], current_ms
                )

                with lock:
                    state.update(p)
                    state["connected"] = True
                    state["mode"] = p["packet"]
                    state["valid"] += 1
                    state["delta_ms"] = delta
                    state["reference_ms"] = self.reference_ms
                    state["reference_ready"] = bool(self.reference_trace)
                    state["reference_source"] = self.reference_source
                    state["reference_samples"] = len(self.reference_trace)
                    valid = state["valid"]

                self.status(
                    f"GT7 LIVE VERBUNDEN · Packet {p['packet']} · "
                    f"{len(raw)} Bytes · gültige Pakete: {valid}"
                )

            except socket.timeout:
                pass
            except Exception as e:
                self.status("UDP-Fehler: " + str(e))

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

        tk.Label(box, text="SPEED DYNAMICS",
                 font=("Segoe UI", 26, "bold"),
                 fg="#d7b45a", bg="#111111").pack(anchor="w")
        tk.Label(box, text="GT7 Race Engineer · Complete Edition",
                 font=("Segoe UI", 11),
                 fg="#aaaaaa", bg="#111111").pack(anchor="w", pady=(0, 22))

        tk.Label(box, text="IP-Adresse deiner PS5",
                 font=("Segoe UI", 10, "bold"),
                 fg="white", bg="#111111").pack(anchor="w")
        self.ip = tk.StringVar(value="192.168.178.200")
        ttk.Entry(box, textvariable=self.ip,
                  font=("Segoe UI", 14)).pack(fill="x", pady=(6, 12))

        tk.Button(box, text="MIT GT7 VERBINDEN",
                  command=self.connect,
                  font=("Segoe UI", 12, "bold"),
                  bg="#d7b45a", fg="#111111",
                  relief="flat", pady=10).pack(fill="x")

        self.msg = tk.StringVar(value="Bereit")
        tk.Label(box, textvariable=self.msg,
                 font=("Segoe UI", 11, "bold"),
                 fg="#dddddd", bg="#111111",
                 wraplength=580, justify="left").pack(anchor="w", pady=(16, 8))

        self.diag = tk.StringVar(
            value="Empfangen: 0    Gültig: 0    Bytes: 0    Paket: 0"
        )
        tk.Label(box, textvariable=self.diag,
                 font=("Consolas", 9),
                 fg="#aaaaaa", bg="#111111").pack(anchor="w", pady=(2, 14))

        self.url = f"http://{lan_ip()}:{WEB_PORT}"
        tk.Label(box, text="Tablet/Handy – im selben WLAN öffnen:",
                 font=("Segoe UI", 9),
                 fg="#888888", bg="#111111").pack(anchor="w")
        tk.Label(box, text=self.url,
                 font=("Consolas", 14, "bold"),
                 fg="#d7b45a", bg="#111111").pack(anchor="w", pady=(3, 7))
        tk.Button(box, text="ADRESSE KOPIEREN",
                  command=self.copy_url,
                  bg="#282828", fg="white",
                  relief="flat", pady=7).pack(fill="x")

        threading.Thread(target=start_web, daemon=True).start()

    def status(self, text):
        with lock:
            d = (
                f"Empfangen: {state['packets']}    "
                f"Gültig: {state['valid']}    "
                f"Bytes: {state['bytes']}    "
                f"Paket: {state['packet_size']}"
            )
        self.root.after(
            0, lambda: (self.msg.set(text), self.diag.set(d))
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
            state.update({
                "connected": False, "mode": "", "packets": 0, "valid": 0,
                "bytes": 0, "packet_size": 0, "delta_ms": None,
                "reference_ms": None, "reference_ready": False,
                "reference_source": "", "reference_samples": 0
            })

        self.bridge = Bridge(ip, self.status)
        self.bridge.start()

    def copy_url(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.url)
        self.msg.set("Adresse kopiert")

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    App().run()
