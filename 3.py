"""
voice_cloner_pro.py

Professional single-file voice-cloner GUI app.

Features:
- Record voice sample or upload a WAV file.
- Paste/type text to synthesize.
- Uses Coqui TTS (voice cloning) when available.
- Falls back to pyttsx3 if TTS unavailable.
- Settings persistence (settings.json): remembers last sample & user prefs.
- Non-blocking UI operations via threading.
- Basic error handling and user guidance.

Requirements:
- Python 3.10 (recommended if you want Coqui TTS)
- pip install sounddevice scipy playsound==1.2.2 pyttsx3
- Optional for real cloning: pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
                              pip install TTS
"""

import os
import json
import threading
import queue
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Audio / TTS imports (some are optional; we import conditionally)
import sounddevice as sd
from scipy.io.wavfile import write
from playsound import playsound

# fallback TTS
try:
    import pyttsx3
except Exception:
    pyttsx3 = None

# try to import Coqui TTS
HAS_TTS = False
TTS = None
try:
    from TTS.api import TTS as CoquiTTSClass
    HAS_TTS = True
    TTS = CoquiTTSClass
except Exception:
    HAS_TTS = False
    TTS = None

# -------------------- Config & Persistence --------------------
APP_NAME = "Voice Cloner Pro"
SETTINGS_FILE = Path.home() / ".voice_cloner_settings.json"
DEFAULT_SETTINGS = {
    "last_voice_sample": "",
    "record_seconds": 10,
    "sample_rate": 44100,
    "tts_model_name": "tts_models/en/vctk/vits"  # default suggestion; replace as needed
}

def load_settings():
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
                DEFAULT_SETTINGS.update(s)
        except Exception:
            pass

def save_settings():
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_SETTINGS, f, indent=2)
    except Exception:
        pass

load_settings()

# -------------------- Utility Helpers --------------------
def safe_play(path):
    """Play audio without crashing the UI (blocking)."""
    try:
        playsound(path)
    except Exception as e:
        messagebox.showerror("Playback Error", f"Could not play audio:\n{e}")

def ensure_wav_path(path: str):
    """Return a path-like string or None if invalid."""
    if path and os.path.exists(path) and path.lower().endswith(".wav"):
        return path
    return None

# -------------------- Worker Thread & Queue --------------------
# We'll use a queue to post status updates from threads back to the main UI.
status_q = queue.Queue()

def post_status(msg: str):
    status_q.put(msg)

# -------------------- Core App Class --------------------
class VoiceClonerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("680x520")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.on_exit)

        # Internal state
        self.voice_sample = DEFAULT_SETTINGS.get("last_voice_sample") or None
        self.record_seconds = int(DEFAULT_SETTINGS.get("record_seconds", 10))
        self.sample_rate = int(DEFAULT_SETTINGS.get("sample_rate", 44100))
        self.tts_model_name = DEFAULT_SETTINGS.get("tts_model_name", "tts_models/en/vctk/vits")
        self.output_file = str(Path.cwd() / "vc_output.wav")
        self._coqui_instance = None  # lazy load

        # Build UI
        self._build_ui()

        # Periodically check status queue
        self.after(200, self._process_status_queue)

    # ---------- UI Construction ----------
    def _build_ui(self):
        pad = 10
        # Title
        title = ttk.Label(self, text=APP_NAME, font=("Segoe UI", 18, "bold"))
        title.pack(pady=(pad, 0))

        # Frame: top controls
        top_frame = ttk.Frame(self)
        top_frame.pack(fill="x", padx=pad, pady=(8, 4))

        # Record and upload buttons
        btn_record = ttk.Button(top_frame, text="🎙️ Record Voice", command=self._on_record)
        btn_upload = ttk.Button(top_frame, text="📂 Upload WAV", command=self._on_upload)
        btn_record.grid(row=0, column=0, padx=(0, 8))
        btn_upload.grid(row=0, column=1, padx=(0, 8))

        # Remembered sample label + path
        self.sample_label_var = tk.StringVar(value=self._get_sample_label_text())
        sample_label = ttk.Label(top_frame, textvariable=self.sample_label_var)
        sample_label.grid(row=0, column=2, sticky="w", padx=(12,0))

        # Frame: settings
        settings_frame = ttk.LabelFrame(self, text="Recording & Model Settings")
        settings_frame.pack(fill="x", padx=pad, pady=(6, 6))

        # Record length
        ttk.Label(settings_frame, text="Record seconds:").grid(row=0, column=0, sticky="e", padx=6, pady=6)
        self.record_spin = ttk.Spinbox(settings_frame, from_=2, to=60, width=6, command=self._on_record_seconds_changed)
        self.record_spin.set(str(self.record_seconds))
        self.record_spin.grid(row=0, column=1, sticky="w", padx=6, pady=6)

        ttk.Label(settings_frame, text="Sample rate:").grid(row=0, column=2, sticky="e", padx=6, pady=6)
        self.sr_combo = ttk.Combobox(settings_frame, values=[8000, 16000, 22050, 32000, 44100, 48000], width=8)
        self.sr_combo.set(str(self.sample_rate))
        self.sr_combo.grid(row=0, column=3, sticky="w", padx=6, pady=6)
        self.sr_combo.bind("<<ComboboxSelected>>", lambda e: self._on_sample_rate_changed())

        ttk.Label(settings_frame, text="TTS model name:").grid(row=1, column=0, sticky="e", padx=6, pady=6)
        self.model_entry = ttk.Entry(settings_frame, width=40)
        self.model_entry.insert(0, self.tts_model_name)
        self.model_entry.grid(row=1, column=1, columnspan=3, sticky="w", padx=6, pady=6)

        # Frame: text input
        text_frame = ttk.LabelFrame(self, text="Text to Speak")
        text_frame.pack(fill="both", expand=True, padx=pad, pady=(0, 6))

        self.text_box = tk.Text(text_frame, wrap="word", height=10)
        self.text_box.pack(fill="both", expand=True, padx=6, pady=6)

        # Helper: sample text button
        helper_frame = ttk.Frame(self)
        helper_frame.pack(fill="x", padx=pad, pady=(0, 6))
        ttk.Button(helper_frame, text="Insert Demo Text", command=self._insert_demo_text).pack(side="left")
        ttk.Button(helper_frame, text="Clear Text", command=lambda: self.text_box.delete("1.0", "end")).pack(side="left", padx=(6,0))

        # Frame: action buttons
        action_frame = ttk.Frame(self)
        action_frame.pack(fill="x", padx=pad, pady=6)

        self.btn_generate = ttk.Button(action_frame, text="🗣️ Generate (Clone Voice)", command=self._on_generate)
        self.btn_generate.pack(side="left")
        ttk.Button(action_frame, text="🔊 Play Last Output", command=self._on_play_output).pack(side="left", padx=(8,0))
        ttk.Button(action_frame, text="⚙️ Show Diagnostics", command=self._show_diagnostics).pack(side="right")

        # Status area
        status_frame = ttk.LabelFrame(self, text="Status")
        status_frame.pack(fill="x", padx=pad, pady=(0, pad))
        self.status_var = tk.StringVar(value="Ready")
        self.status_label = ttk.Label(status_frame, textvariable=self.status_var, anchor="w")
        self.status_label.pack(fill="x", padx=6, pady=6)

        # Small note about TTS availability
        tts_note = "Coqui TTS available" if HAS_TTS else "Coqui TTS not installed — app will fall back to system TTS"
        self.tts_status_label = ttk.Label(self, text=tts_note, foreground="gray")
        self.tts_status_label.pack(pady=(0,6))

    def _get_sample_label_text(self):
        if self.voice_sample and os.path.exists(self.voice_sample):
            return f"Sample: {os.path.basename(self.voice_sample)}"
        return "No voice sample selected"

    # ---------- UI Callbacks ----------
    def _on_record_seconds_changed(self):
        try:
            v = int(self.record_spin.get())
            self.record_seconds = v
            DEFAULT_SETTINGS["record_seconds"] = v
            save_settings()
        except Exception:
            pass

    def _on_sample_rate_changed(self):
        try:
            v = int(self.sr_combo.get())
            self.sample_rate = v
            DEFAULT_SETTINGS["sample_rate"] = v
            save_settings()
        except Exception:
            pass

    def _on_record(self):
        # Start recording in a separate thread to avoid blocking UI
        t = threading.Thread(target=self._record_worker, daemon=True)
        t.start()

    def _record_worker(self):
        try:
            post_status("Recording...")
            self._set_status("Recording...")
            duration = self.record_seconds
            samplerate = self.sample_rate
            # Record with sounddevice
            rec = sd.rec(int(duration * samplerate), samplerate=samplerate, channels=1, dtype='int16')
            sd.wait()
            outpath = str(Path.cwd() / "myvoice_sample.wav")
            write(outpath, samplerate, rec)
            self.voice_sample = outpath
            self.sample_label_var.set(self._get_sample_label_text())
            DEFAULT_SETTINGS["last_voice_sample"] = self.voice_sample
            save_settings()
            post_status(f"Saved voice sample: {outpath}")
            self._set_status("Recording complete.")
        except Exception as e:
            post_status(f"Recording failed: {e}")
            self._set_status("Recording failed.")
            messagebox.showerror("Recording Error", f"Could not record:\n{e}")

    def _on_upload(self):
        file_path = filedialog.askopenfilename(title="Select voice WAV file", filetypes=[("WAV files", "*.wav")])
        if file_path:
            if not file_path.lower().endswith(".wav"):
                messagebox.showerror("Invalid file", "Please select a .wav file.")
                return
            self.voice_sample = file_path
            self.sample_label_var.set(self._get_sample_label_text())
            DEFAULT_SETTINGS["last_voice_sample"] = self.voice_sample
            save_settings()
            self._set_status(f"Uploaded voice sample: {os.path.basename(file_path)}")

    def _on_generate(self):
        # Kick off a worker thread that will synthesize speech
        text = self.text_box.get("1.0", "end").strip()
        if not text:
            messagebox.showerror("No text", "Please enter or paste text to synthesize.")
            return
        if not (self.voice_sample and os.path.exists(self.voice_sample)):
            if not HAS_TTS:
                # fallback allowed: use pyttsx3 with no sample
                if not pyttsx3:
                    messagebox.showerror("No TTS engine", "Neither Coqui TTS nor pyttsx3 is available.")
                    return
                else:
                    if not messagebox.askyesno("No voice sample", "No voice sample found. Continue with system TTS?"):
                        return
            else:
                if not messagebox.askyesno("No voice sample", "No voice sample found. Continue with system TTS? (Cloning requires a sample)"):
                    return

        # Save model setting
        self.tts_model_name = self.model_entry.get().strip() or self.tts_model_name
        DEFAULT_SETTINGS["tts_model_name"] = self.tts_model_name
        save_settings()

        self.btn_generate.config(state="disabled")
        t = threading.Thread(target=self._generate_worker, args=(text,), daemon=True)
        t.start()

    def _generate_worker(self, text):
        try:
            self._set_status("Preparing synthesis...")
            if HAS_TTS:
                # Lazy initialize Coqui TTS instance (might download model first time)
                if self._coqui_instance is None:
                    self._set_status(f"Loading Coqui model: {self.tts_model_name} (this may take time)...")
                    try:
                        self._coqui_instance = TTS(model_name=self.tts_model_name, progress_bar=True, gpu=False)
                    except Exception as e:
                        post_status(f"Failed to load Coqui model: {e}")
                        # fall back
                        self._coqui_instance = None
                        raise e

                # Use coqui to generate audio in user's voice sample
                self._set_status("Generating audio with Coqui TTS...")
                out = self.output_file
                self._coqui_instance.tts_to_file(text=text, speaker_wav=self.voice_sample, file_path=out)
                post_status(f"Generated (Coqui) -> {out}")
                self._set_status("Synthesis complete (Coqui). Playing output...")
                safe_play(out)
                self._set_status("Ready")
            else:
                # fallback to pyttsx3
                if not pyttsx3:
                    raise RuntimeError("pyttsx3 not installed; cannot synthesize.")
                self._set_status("Generating with local system TTS (pyttsx3)...")
                engine = pyttsx3.init()
                engine.save_to_file(text, self.output_file)
                engine.runAndWait()
                post_status(f"Generated (pyttsx3) -> {self.output_file}")
                self._set_status("Synthesis complete (pyttsx3). Playing output...")
                safe_play(self.output_file)
                self._set_status("Ready")
        except Exception as e:
            post_status(f"Synthesis failed: {e}")
            self._set_status("Synthesis failed.")
            messagebox.showerror("Synthesis Error", f"Failed to synthesize voice:\n{e}")
        finally:
            # Re-enable button
            self.btn_generate.config(state="normal")

    def _on_play_output(self):
        if os.path.exists(self.output_file):
            t = threading.Thread(target=safe_play, args=(self.output_file,), daemon=True)
            t.start()
            self._set_status(f"Playing last output: {self.output_file}")
        else:
            messagebox.showinfo("No output", "No generated output found. Generate first.")

    def _insert_demo_text(self):
        demo = (
            "Hello, this is a demo. Type or paste any text you want to be spoken. "
            "For best cloning results, provide a few minutes of clean recorded audio in the sample."
        )
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", demo)

    def _show_diagnostics(self):
        diag = [
            f"Python: {os.sys.version.splitlines()[0]}",
            f"Coqui TTS installed: {HAS_TTS}",
            f"pyttsx3 installed: {pyttsx3 is not None}",
            f"Voice sample: {self.voice_sample}",
            f"Output file: {self.output_file}",
            f"TTS model name: {self.tts_model_name}",
            f"Record seconds: {self.record_seconds}",
            f"Sample rate: {self.sample_rate}",
        ]
        messagebox.showinfo("Diagnostics", "\n".join(diag))

    def _set_status(self, text):
        # Update UI label from any thread by using .after
        def _u():
            self.status_var.set(text)
        self.after(0, _u)

    def _process_status_queue(self):
        while not status_q.empty():
            try:
                msg = status_q.get_nowait()
                # for now we only set in the label; could also log to file
                self.status_var.set(msg)
            except queue.Empty:
                break
        self.after(200, self._process_status_queue)

    def on_exit(self):
        # Save last used settings
        DEFAULT_SETTINGS["last_voice_sample"] = self.voice_sample or ""
        DEFAULT_SETTINGS["record_seconds"] = self.record_seconds
        DEFAULT_SETTINGS["sample_rate"] = self.sample_rate
        DEFAULT_SETTINGS["tts_model_name"] = self.tts_model_name
        save_settings()
        self.destroy()

# -------------------- Main --------------------
def main():
    app = VoiceClonerApp()
    app.mainloop()

if __name__ == "__main__":
    main()
