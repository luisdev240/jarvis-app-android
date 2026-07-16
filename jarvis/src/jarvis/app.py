import os
import json
import threading
import re
import io
import wave
import base64
from datetime import datetime, timedelta
from kivy.app import App
from kivy.lang import Builder
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.widget import Widget
from kivy.uix.scrollview import ScrollView
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.textinput import TextInput
from kivy.uix.button import Button
from kivy.uix.screenmanager import ScreenManager, Screen, FadeTransition
from kivy.properties import NumericProperty, ListProperty, StringProperty, BooleanProperty
try:
    from jnius import autoclass, cast, jarray
    ANDROID = True
except Exception:
    ANDROID = False
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.animation import Animation

# ============================================================
# CONFIGURATION GROQ & MODÈLES
# ============================================================
GROQ_API_KEY = "gsk_votre_cle_api_ici"  # ⚠️ REMPLACEZ PAR VOTRE CLÉ
WHISPER_MODEL = "whisper-large-v3-turbo"
ORPHEUS_MODEL = "canopylabs/orpheus-3b-0.1-pretrained"
WAKE_WORD = "jarvis"

CONFIG_FILE = "config.json"
CONVERSATIONS_FILE = "conversations.json"
APP_VERSION = "4.0"
VERSION_CHECK_URL = "https://raw.githubusercontent.com/myduckyfishing-png/jarvis-app-android/main/version.txt"
APK_DOWNLOAD_URL = "https://github.com/myduckyfishing-png/jarvis-app-android/releases/latest/download/jarvisia.apk"

THEMES = {
    "Cyan": (0, 0.85, 1),
    "Rouge": (1, 0.15, 0.15),
    "Violet": (0.65, 0.2, 1),
    "Vert": (0.15, 0.9, 0.4),
    "Orange": (1, 0.55, 0.1),
}
LANGUAGES = ["Français", "Anglais", "Espagnol", "Allemand"]

def https_get(url, timeout=8):
    """Fait une requete HTTPS sans verifier le certificat SSL."""
    import urllib.request
    import ssl
    context = ssl._create_unverified_context()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
    )
    with urllib.request.urlopen(req, timeout=timeout, context=context) as response:
        return response.read().decode("utf-8")

def encode_multipart_formdata(fields, files):
    import uuid
    boundary = uuid.uuid4().hex
    body = bytearray()
    
    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("ascii"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"))
        body.extend(f"{value}\r\n".encode("utf-8"))
        
    for name, (filename, content_type, data) in files.items():
        body.extend(f"--{boundary}\r\n".encode("ascii"))
        body.extend(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode("ascii"))
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("ascii"))
        body.extend(data)
        body.extend(b"\r\n")
        
    body.extend(f"--{boundary}--\r\n".encode("ascii"))
    content_type = f"multipart/form-data; boundary={boundary}"
    return content_type, bytes(body)

COLOR_BG = (0.01, 0.03, 0.05, 1)
Window.clearcolor = COLOR_BG
Window.softinput_mode = "below_target"

# ============================================================
# SERVICES INTELLIGENTS (RDV, LISTE, WHISPER, ORPHEUS)
# ============================================================

class NotificationService:
    @staticmethod
    def schedule_reminder(title, target_datetime):
        if not ANDROID:
            print(f"[SIMULATION PC] Notifications prévues pour '{title}' le {target_datetime}")
            return
        
        try:
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            activity = PythonActivity.mActivity
            Context = autoclass("android.content.Context")
            AlarmManager = autoclass("android.app.AlarmManager")
            PendingIntent = autoclass("android.app.PendingIntent")
            Intent = autoclass("android.content.Intent")
            
            now_ms = int(datetime.now().timestamp() * 1000)
            
            # NOUVEAUX DÉLAIS DE NOTIFICATION : 24h, 12h, 1h, 45min, 30min, 15min
            delays_minutes = [1440, 720, 60, 45, 30, 15]
            
            for i, delay_min in enumerate(delays_minutes):
                reminder_time = target_datetime - timedelta(minutes=delay_min)
                target_ms = int(reminder_time.timestamp() * 1000)
                
                if target_ms <= now_ms:
                    continue
                
                intent = Intent(activity.getApplicationContext(), activity.getClass())
                intent.putExtra("notification_title", title)
                intent.putExtra("notification_delay", f"{delay_min} min")
                intent.putExtra("request_code", i + 100)
                
                pending_intent = PendingIntent.getActivity(
                    activity, i + 100, intent,
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
                )
                
                alarm_manager = activity.getSystemService(Context.ALARM_SERVICE)
                alarm_manager.setExact(AlarmManager.RTC_WAKEUP, target_ms, pending_intent)
                
            print(f"[NOTIF] 6 rappels programmés pour '{title}' à {target_datetime.strftime('%H:%M')}")
            
        except Exception as e:
            print(f"[NOTIF ERROR] {e}")


class AppointmentDetector:
    DAYS_FR = {"lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3,
               "vendredi": 4, "samedi": 5, "dimanche": 6}
    
    KEYWORDS = ["rendez vous", "rdv", "réunion", "appointment", "meeting",
                "consultation", "dentiste", "médecin", "coiffeur", "banque",
                "entretien", "cours", "examen", "vol", "train", "avion"]
    
    @classmethod
    def parse_appointment(cls, text):
        text_lower = text.lower()
        
        if not any(kw in text_lower for kw in cls.KEYWORDS):
            return None
        
        now = datetime.now()
        target_date = now.date()
        target_hour = None
        title = ""
        
        title_patterns = [
            r'(?:pour|chez|au|à)\s+([A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜŸ][\wÀÂÄÉÈÊËÎÏÔÖÙÛÜŸ\-]+(?:\s+[A-ZÀÂÄÉÈÊËÎÔÖÙÛÜŸ][\wÀÂÄÉÈÊËÎÏÔÖÙÛÜŸ\-]+)*)',
            r'(?:rdv|rappel)\s+(?:avec\s+)?([A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜŸ][\wÀÂÄÉÈÊËÎÏÔÖÙÛÜ\-]+)',
        ]
        for pattern in title_patterns:
            match = re.search(pattern, text)
            if match:
                title = match.group(1).strip()
                break
        
        if not title:
            words = text.split()
            for w in words:
                if w[0].isupper() and len(w) > 2 and w.lower() not in ['jai', 'j\'ai', 'demain']:
                    title = w.rstrip('.,!?')
                    break
            if not title:
                title = "Rendez-vous"
        
        if "demain" in text_lower or "tomorrow" in text_lower:
            target_date = now.date() + timedelta(days=1)
        elif "après demain" in text_lower:
            target_date = now.date() + timedelta(days=2)
        elif "aujourd'hui" in text_lower or "today" in text_lower:
            target_date = now.date()
        else:
            for day_name, day_num in cls.DAYS_FR.items():
                if day_name in text_lower:
                    current_day = now.weekday()
                    days_ahead = (day_num - current_day) % 7
                    if days_ahead == 0:
                        days_ahead = 7
                    target_date = now.date() + timedelta(days=days_ahead)
                    break
        
        time_patterns = [r'(\d{1,2})h(\d{2})?', r'(\d{1,2}):(\d{2})', r'(\d{1,2})\s*(?:am|pm)']
        
        for pattern in time_patterns:
            match = re.search(pattern, text_lower)
            if match:
                hour = int(match.group(1))
                try:
                    minute = int(match.group(2)) if len(match.groups()) >= 2 and match.group(2) else 0
                except IndexError:
                    minute = 0
                
                if 'pm' in text_lower and hour < 12:
                    hour += 12
                elif 'am' in text_lower and hour == 12:
                    hour = 0
                
                target_hour = hour
                target_minute = minute
                break
        
        if target_hour is None:
            return None
        
        try:
            target_datetime = datetime.combine(target_date, datetime.min.time().replace(hour=target_hour, minute=target_minute))
        except ValueError:
            return None
        
        if target_datetime <= now and "aujourd'hui" not in text_lower:
            target_datetime += timedelta(days=1)
        
        return {"title": title, "datetime": target_datetime, "formatted": target_datetime.strftime("%d/%m/%Y à %H:%M")}


class ShoppingListDetector:
    KEYWORDS = ["ajoute", "ajouter", "met", "mettre", "note", "noter", "liste", "courses", "acheter", "prend", "prendre"]
    STOP_WORDS = ["a", "au", "aux", "de", "des", "du", "la", "le", "les", "un", "une", "dans", "sur", "pour", "et", "ou", "est"]
    
    @classmethod
    def parse_shopping_item(cls, text):
        text_lower = text.lower()
        has_keyword = any(kw in text_lower for kw in cls.KEYWORDS)
        has_list_ref = any(word in text_lower for word in ["liste", "courses", "acheter"])
        
        if not (has_keyword and has_list_ref):
            return None
        
        item = ""
        words = text.split()
        
        for i, word in enumerate(words):
            if word.lower() in cls.KEYWORDS:
                remaining = words[i+1:]
                while remaining and remaining[0].lower() in cls.STOP_WORDS:
                    remaining.pop(0)
                item = " ".join(remaining).strip().rstrip('.,!?')
                break
        
        if not item:
            match = re.search(r'(?:liste|courses|acheter)\s+(.+)', text, re.IGNORECASE)
            if match:
                item = match.group(1).strip().rstrip('.,!?')
                parts = item.split()
                while parts and parts[0].lower() in cls.STOP_WORDS:
                    parts.pop(0)
                item = " ".join(parts)
        
        if not item or len(item) < 2:
            return None
        
        # Nettoyage des suffixes liés à la liste
        item_lower = item.lower()
        suffixes = [
            " à la liste de courses", " à la liste", " de courses", 
            " sur la liste de courses", " sur la liste", 
            " dans la liste de courses", " dans la liste",
            " à ma liste", " sur ma liste", " dans ma liste"
        ]
        for suffix in suffixes:
            if item_lower.endswith(suffix):
                item = item[:-len(suffix)].strip()
                break
                
        if not item or len(item) < 2:
            return None
            
        return item[0].upper() + item[1:] if len(item) > 1 else item.upper()


class VoiceService:
    """Gère Whisper (STT) et Orpheus (TTS) via Groq API"""
    _tts = None
    _player = None
    
    @staticmethod
    def transcribe_audio(pcm_data):
        """Transcrit PCM 16kHz mono en texte via Whisper"""
        try:
            app = App.get_running_app()
            api_key = app.get_saved_key() if app else None
            if not api_key:
                api_key = GROQ_API_KEY
                
            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(pcm_data)
            
            wav_buffer.seek(0)
            wav_data = wav_buffer.read()
            
            fields = {
                "model": WHISPER_MODEL,
                "response_format": "text"
            }
            files = {
                "file": ("audio.wav", "audio/wav", wav_data)
            }
            content_type, body = encode_multipart_formdata(fields, files)
            
            import urllib.request
            req = urllib.request.Request(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                data=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": content_type
                }
            )
            
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.read().decode("utf-8").strip()
        except Exception as e:
            print(f"[WHISPER ERROR] {e}")
            return None

    @staticmethod
    def speak_text(text):
        """Synthèse vocale : Orpheus (Android) ou pyttsx3/SAPI (PC)"""
        clean_text = re.sub(r'\[.*?\]|\*+|#{1,6}\s', '', text).strip()
        if not clean_text:
            return

        if not ANDROID:
            # --- TTS PC : Groq TTS (type Siri) en priorité, PowerShell SAPI en fallback ---
            def _speak_pc():
                try:
                    # Récupérer la clé API
                    app = App.get_running_app()
                    api_key = app.get_saved_key() if app else None
                    if not api_key:
                        api_key = GROQ_API_KEY

                    if api_key and not api_key.startswith("gsk_votre"):
                        # --- Groq TTS API : voix masculine naturelle (type Siri) ---
                        import urllib.request, tempfile, winsound
                        req = urllib.request.Request(
                            "https://api.groq.com/openai/v1/audio/speech",
                            data=json.dumps({
                                "model": "playai-tts",
                                "voice": "Fritz-PlayAI",  # voix masculine grave et naturelle
                                "input": clean_text,
                                "response_format": "wav"
                            }).encode(),
                            headers={
                                "Authorization": f"Bearer {api_key}",
                                "Content-Type": "application/json"
                            }
                        )
                        with urllib.request.urlopen(req, timeout=15) as resp:
                            wav_data = resp.read()
                        tmp_file = os.path.join(tempfile.gettempdir(), "jarvis_tts.wav")
                        with open(tmp_file, "wb") as f:
                            f.write(wav_data)
                        winsound.PlaySound(tmp_file, winsound.SND_FILENAME)
                        return
                except Exception as e:
                    print(f"[TTS GROQ PC] {e} — bascule sur SAPI")

                # --- Fallback PowerShell SAPI (Windows natif, aucune install requise) ---
                try:
                    import subprocess
                    escaped = clean_text.replace("'", "''").replace('"', '`"')
                    script = (
                        "Add-Type -AssemblyName System.Speech; "
                        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                        "$s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Male, [System.Speech.Synthesis.VoiceAge]::Adult); "
                        "$s.Rate = 1; "
                        "$s.Volume = 100; "
                        f"$s.Speak(\"{escaped}\")"
                    )
                    subprocess.Popen(
                        ["powershell", "-WindowStyle", "Hidden", "-Command", script],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                except Exception as e:
                    print(f"[TTS PC ERROR] {e}")
            threading.Thread(target=_speak_pc, daemon=True).start()
            return
        try:
                
            app = App.get_running_app()
            api_key = app.get_saved_key() if app else None
            if not api_key:
                api_key = GROQ_API_KEY
                
            import urllib.request
            req = urllib.request.Request(
                "https://api.groq.com/openai/v1/chat/completions",
                data=json.dumps({
                    "model": ORPHEUS_MODEL,
                    "messages": [{"role": "user", "content": clean_text}],
                    "modalities": ["text", "audio"],
                    "audio": {"voice": "onyx", "format": "wav"}
                }).encode(),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            )
            
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                audio_b64 = data.get("choices", [{}])[0].get("message", {}).get("audio", {}).get("data")
                
                if audio_b64:
                    temp_path = os.path.join(app.user_data_dir, "temp_voice.wav")
                    with open(temp_path, "wb") as f:
                        f.write(base64.b64decode(audio_b64))
                    MediaPlayer = autoclass("android.media.MediaPlayer")
                    player = MediaPlayer()
                    player.setDataSource(temp_path)
                    player.prepare()
                    player.start()
                    VoiceService._player = player
                    print("[ORPHEUS] Lecture démarrée")
                    
        except Exception as e:
            print(f"[ORPHEUS ERROR] {e}")
            try:
                TextToSpeech = autoclass("android.speech.tts.TextToSpeech")
                Locale = autoclass("java.util.Locale")
                if VoiceService._tts is None:
                    activity = autoclass("org.kivy.android.PythonActivity").mActivity
                    VoiceService._tts = TextToSpeech(activity, None)
                try:
                    VoiceService._tts.setLanguage(Locale.FRENCH)
                except Exception:
                    pass
                VoiceService._tts.speak(clean_text, 0, None, None)
            except Exception as tts_err:
                print(f"[TTS FALLBACK ERROR] {tts_err}")


class JarvisBackgroundService:
    """Service d'écoute permanente en arrière-plan"""
    
    def __init__(self):
        self.is_listening = False
        self.audio_record = None

    def start_listening(self):
        if not ANDROID:
            print("[SERVICE] Mode simulation PC - écoute inactive")
            return

        try:
            AudioRecord = autoclass("android.media.AudioRecord")
            AudioSource = autoclass("android.media.MediaRecorder$AudioSource")
            AudioFormat = autoclass("android.media.AudioFormat")

            sample_rate = 16000
            channel_config = AudioFormat.CHANNEL_IN_MONO
            audio_format = AudioFormat.ENCODING_PCM_16BIT
            buffer_size = AudioRecord.getMinBufferSize(sample_rate, channel_config, audio_format)

            self.audio_record = AudioRecord(AudioSource.MIC, sample_rate, channel_config, audio_format, buffer_size * 4)
            self.audio_record.startRecording()
            self.is_listening = True

            threading.Thread(target=self._listen_loop, daemon=True).start()
            print("[SERVICE] Écoute arrière-plan démarrée")

        except Exception as e:
            print(f"[SERVICE ERREUR] {e}")

    def _listen_loop(self):
        buffer_size = 16000 * 3 * 2
        j_buffer = jarray('b')(buffer_size)

        while self.is_listening:
            try:
                bytes_read = self.audio_record.read(j_buffer, 0, buffer_size)
                if bytes_read > 0:
                    pcm_data = bytes(j_buffer[:bytes_read])
                    text = VoiceService.transcribe_audio(pcm_data)
                    if text and WAKE_WORD in text.lower():
                        self._wake_up(text)
            except Exception as e:
                print(f"[SERVICE LOOP ERREUR] {e}")

    def _wake_up(self, detected_text):
        print(f"[WAKE UP] Détecté: '{detected_text}'")

        if ANDROID:
            try:
                Intent = autoclass("android.content.Intent")
                PythonActivity = autoclass("org.kivy.android.PythonActivity")
                intent = Intent(PythonActivity.mActivity, PythonActivity.mActivity.getClass())
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)
                PythonActivity.mActivity.startActivity(intent)
            except Exception as e:
                print(f"[WAKE UP ERREUR] {e}")

        Clock.schedule_once(lambda dt: self._process_command(detected_text), 1.5)

    def _process_command(self, text):
        app = App.get_running_app()
        if not app or not app.root_widget:
            return
            
        command = text.lower().replace(WAKE_WORD, "").strip()
        root = app.root_widget
        
        if command:
            root.ids.msg_input.text = command
            root.send_message()
            Clock.schedule_once(lambda dt: self._trigger_orpheus(root), 4.0)
        else:
            root.add_message("Bonjour ! Je suis là.", is_user=False)
            VoiceService.speak_text("Bonjour ! Je suis là.")

    def _trigger_orpheus(self, root):
        try:
            last_msg = ""
            for child in reversed(root.ids.chat_box.children):
                if hasattr(child, 'children') and len(child.children) > 0:
                    label = child.children[0]
                    if hasattr(label, 'text'):
                        last_msg = label.text
                        break
            
            if last_msg and "JARVIS" in last_msg:
                VoiceService.speak_text(last_msg)
        except Exception as e:
            print(f"[ORPHEUS TRIGGER ERROR] {e}")

    def stop_listening(self):
        self.is_listening = False
        if hasattr(self, 'audio_record') and self.audio_record:
            try:
                self.audio_record.stop()
                self.audio_record.release()
            except Exception:
                pass


KV = """
#:import dp kivy.metrics.dp

<GlowPanel@BoxLayout>:
    canvas.before:
        Color:
            rgba: 0.03, 0.08, 0.11, 1
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(14)]
        Color:
            rgba: app.primary_color[0], app.primary_color[1], app.primary_color[2], 0.18
        Line:
            rounded_rectangle: (self.x - dp(1), self.y - dp(1), self.width + dp(2), self.height + dp(2), dp(15))
            width: dp(2.4)
        Color:
            rgba: app.primary_color[0], app.primary_color[1], app.primary_color[2], 0.7
        Line:
            rounded_rectangle: (self.x, self.y, self.width, self.height, dp(14))
            width: 1.1

<BorderedField@Button>:
    line_color: 0, 0.85, 1, 0.8
    fill_color: 0.04, 0.06, 0.08, 1
    background_normal: ""
    background_down: ""
    background_color: 0, 0, 0, 0
    halign: "left"
    valign: "middle"
    padding: dp(12), 0
    text_size: self.width - dp(24), self.height
    canvas.before:
        Color:
            rgba: self.fill_color
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(4)]
        Color:
            rgba: self.line_color
        Line:
            rounded_rectangle: (self.x, self.y, self.width, self.height, dp(4))
            width: dp(1.1)

<PillCapsule@Button>:
    line_color: 0, 0.85, 1, 1
    fill_color: 0.05, 0.08, 0.1, 1
    background_normal: ""
    background_down: ""
    background_color: 0, 0, 0, 0
    canvas.before:
        Color:
            rgba: self.fill_color
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [self.height / 2]
        Color:
            rgba: self.line_color
        Line:
            rounded_rectangle: (self.x, self.y, self.width, self.height, self.height / 2)
            width: dp(1.3)

<ChatBubble@BoxLayout>:
    size_hint_y: None
    height: self.minimum_height
    bg_color: 0, 0.85, 1, 0.12
    line_color: 0, 0.85, 1, 0.6
    canvas.before:
        Color:
            rgba: self.bg_color
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(12)]
        Color:
            rgba: self.line_color
        Line:
            rounded_rectangle: (self.x, self.y, self.width, self.height, dp(12))
            width: 1

<ArcRing@Widget>:
    angle: 0
    size_hint: None, None
    size: dp(120), dp(120)
    canvas:
        Color:
            rgba: 0, 0.85, 1, 0.25
        Line:
            circle: (self.center_x, self.center_y, dp(55))
            width: dp(1.5)
        Color:
            rgba: 1, 0.7, 0.15, 0.9
        Line:
            circle: (self.center_x, self.center_y, dp(55), self.angle, self.angle + 90)
            width: dp(3)
        Color:
            rgba: 0, 0.85, 1, 0.9
        Line:
            circle: (self.center_x, self.center_y, dp(55), self.angle + 180, self.angle + 270)
            width: dp(3)
        Color:
            rgba: 0, 0.85, 1, 1
        Line:
            circle: (self.center_x, self.center_y, dp(36))
            width: dp(1.2)
        Color:
            rgba: 1, 1, 1, 1
        Ellipse:
            pos: self.center_x - dp(4), self.center_y - dp(4)
            size: dp(8), dp(8)

<Orb>:
    angle: 0
    energy: 0.45
    size_hint: None, None
    size: dp(160), dp(160)
    canvas:
        Color:
            rgba: 0, 0.85, 1, self.energy * 0.15
        Ellipse:
            pos: self.center_x - dp(75) * self.energy, self.center_y - dp(75) * self.energy
            size: dp(150) * self.energy, dp(150) * self.energy
        Color:
            rgba: 0, 0.85, 1, self.energy * 0.08
        Ellipse:
            pos: self.center_x - dp(90) * self.energy, self.center_y - dp(90) * self.energy
            size: dp(180) * self.energy, dp(180) * self.energy
        Color:
            rgba: 0, 0.85, 1, 0.3
        Line:
            circle: (self.center_x, self.center_y, dp(65))
            width: dp(1)
        PushMatrix
        Rotate:
            angle: self.angle
            origin: self.center
        Color:
            rgba: 0, 0.85, 1, 0.6
        Line:
            ellipse: (self.center_x - dp(55), self.center_y - dp(25), dp(110), dp(50))
            width: dp(1.5)
        Color:
            rgba: 1, 0.7, 0.15, 0.8
        Line:
            circle: (self.center_x, self.center_y, dp(55), 0, 45)
            width: dp(3)
        Line:
            circle: (self.center_x, self.center_y, dp(55), 180, 225)
            width: dp(3)
        PopMatrix
        PushMatrix
        Rotate:
            angle: -self.angle * 1.5 + 45
            origin: self.center
        Color:
            rgba: 0, 0.85, 1, 0.4
        Line:
            ellipse: (self.center_x - dp(45), self.center_y - dp(15), dp(90), dp(30))
            width: dp(1)
        PopMatrix
        PushMatrix
        Rotate:
            angle: self.angle * 2
            origin: self.center
        Color:
            rgba: 1, 1, 1, 0.9
        Ellipse:
            pos: self.center_x + dp(40) - dp(2), self.center_y - dp(2)
            size: dp(4), dp(4)
        Ellipse:
            pos: self.center_x - dp(40) - dp(2), self.center_y - dp(2)
            size: dp(4), dp(4)
        PopMatrix
        Color:
            rgba: 0, 0.85, 1, 0.85
        Line:
            circle: (self.center_x, self.center_y, dp(35))
            width: dp(1)
        Color:
            rgba: 1, 1, 1, 0.95
        Ellipse:
            pos: self.center_x - dp(8), self.center_y - dp(8)
            size: dp(16), dp(16)
        Color:
            rgba: 0, 0.85, 1, 1
        Ellipse:
            pos: self.center_x - dp(3), self.center_y - dp(3)
            size: dp(6), dp(6)

<BootScreen>:
    name: "boot"
    BoxLayout:
        orientation: "vertical"
        padding: dp(30)
        spacing: dp(10)
        canvas.before:
            Color:
                rgba: 0.01, 0.03, 0.05, 1
            Rectangle:
                pos: self.pos
                size: self.size
        Widget:
            size_hint_y: 0.18
        ArcRing:
            id: arc_ring
            pos_hint: {"center_x": 0.5}
            size_hint_x: None
            x: self.parent.center_x - self.width / 2 if self.parent else 0
        Label:
            text: "[b]J.A.R.V.I.S[/b]"
            markup: True
            font_size: "30sp"
            color: app.primary_color[0], app.primary_color[1], app.primary_color[2], 1
            size_hint_y: None
            height: dp(46)
        Widget:
            size_hint_y: None
            height: dp(2)
            canvas.before:
                Color:
                    rgba: 1, 0.7, 0.15, 0.8
                Rectangle:
                    pos: self.x + self.width * 0.3, self.y
                    size: self.width * 0.4, dp(2)
        Widget:
            size_hint_y: None
            height: dp(14)
        Label:
            id: boot_line
            text: ""
            markup: True
            font_size: "13sp"
            color: 0.5, 0.85, 0.95, 1
            size_hint_y: None
            height: dp(26)
        BoxLayout:
            size_hint_y: None
            height: dp(6)
            padding: dp(40), 0
            canvas.before:
                Color:
                    rgba: 0, 0.3, 0.4, 1
                Rectangle:
                    pos: self.x + dp(40), self.y
                    size: self.width - dp(80), self.height
                Color:
                    rgba: 1, 0.7, 0.15, 1
                Rectangle:
                    pos: self.x + dp(40), self.y
                    size: (self.width - dp(80)) * root.progress, self.height
        Widget:
            size_hint_y: 0.45

<ChatScreen>:
    name: "chat"

<RootWidget>:
    orientation: "vertical"
    padding: dp(12)
    spacing: dp(12)
    canvas.before:
        Color:
            rgba: 0.01, 0.03, 0.05, 1
        Rectangle:
            pos: self.pos
            size: self.size
        Color:
            rgba: app.primary_color[0], app.primary_color[1], app.primary_color[2], 0.035
        Line:
            points: [self.x, self.y + self.height * 0.25, self.x + self.width, self.y + self.height * 0.25]
            width: 1
        Line:
            points: [self.x, self.y + self.height * 0.5, self.x + self.width, self.y + self.height * 0.5]
            width: 1
        Line:
            points: [self.x, self.y + self.height * 0.75, self.x + self.width, self.y + self.height * 0.75]
            width: 1
    GlowPanel:
        size_hint_y: None
        height: dp(78)
        padding: dp(12)
        spacing: dp(8)
        Button:
            text: "Conv"
            size_hint: None, None
            size: dp(46), dp(46)
            font_size: "20sp"
            bold: True
            background_normal: ""
            background_color: 0, 0, 0, 0
            color: app.primary_color[0], app.primary_color[1], app.primary_color[2], 1
            canvas.before:
                Color:
                    rgba: 0.03, 0.05, 0.07, 1
                RoundedRectangle:
                    pos: self.pos
                    size: self.size
                    radius: [dp(6)]
                Color:
                    rgba: app.primary_color[0], app.primary_color[1], app.primary_color[2], 0.9
                Line:
                    rounded_rectangle: (self.x, self.y, self.width, self.height, dp(6))
                    width: 1.2
            on_release: root.open_history()
        BoxLayout:
            orientation: "vertical"
            Label:
                id: clock_caption
                text: "[size=10sp]\u25C6 LOCAL_TIME[/size]"
                markup: True
                color: app.primary_color[0], app.primary_color[1], app.primary_color[2], 0.7
                halign: "left"
                valign: "bottom"
                text_size: self.size
                size_hint_y: None
                height: dp(16)
            Label:
                id: clock_label
                text: "00:00:00"
                font_size: "26sp"
                bold: True
                color: app.primary_color[0], app.primary_color[1], app.primary_color[2], 1
                halign: "left"
                valign: "middle"
                text_size: self.size
            Label:
                id: date_label
                text: "--/--/----"
                font_size: "11sp"
                color: 0.55, 0.85, 0.9, 0.8
                halign: "left"
                valign: "top"
                text_size: self.size
                size_hint_y: None
                height: dp(16)
        Button:
            text: "MENU"
            size_hint: None, None
            size: dp(86), dp(38)
            pos_hint: {"center_y": 0.5}
            bold: True
            font_size: "12sp"
            background_normal: ""
            background_color: 0, 0, 0, 0
            color: app.primary_color[0], app.primary_color[1], app.primary_color[2], 1
            canvas.before:
                Color:
                    rgba: 0.03, 0.05, 0.07, 1
                RoundedRectangle:
                    pos: self.pos
                    size: self.size
                    radius: [dp(6)]
                Color:
                    rgba: app.primary_color[0], app.primary_color[1], app.primary_color[2], 0.9
                Line:
                    rounded_rectangle: (self.x, self.y, self.width, self.height, dp(6))
                    width: 1.2
            on_release: root.open_menu()
    Label:
        id: status_label
        text: "[color=ff8c1a]\u25CF[/color] systeme en ligne"
        markup: True
        font_size: "11sp"
        size_hint_y: None
        height: dp(18)
        color: 0.6, 0.85, 0.9, 1
    Orb:
        id: orb
        size_hint_y: None
        height: dp(170)
        pos_hint: {"center_x": 0.5}
    ScrollView:
        id: scroll
        bar_width: dp(4)
        bar_color: app.primary_color[0], app.primary_color[1], app.primary_color[2], 0.6
        do_scroll_x: False
        BoxLayout:
            id: chat_box
            orientation: "vertical"
            size_hint_y: None
            height: self.minimum_height
            spacing: dp(10)
            padding: dp(6)
    GlowPanel:
        orientation: "vertical"
        size_hint_y: None
        height: dp(92) + (dp(54) if root.pending_image_path else 0)
        padding: dp(10)
        spacing: dp(6)
        BoxLayout:
            id: image_preview_row
            size_hint_y: None
            height: dp(46) if root.pending_image_path else 0
            opacity: 1 if root.pending_image_path else 0
            spacing: dp(8)
            Image:
                id: image_preview
                source: root.pending_image_path or ""
                size_hint_x: None
                width: dp(46) if root.pending_image_path else 0
                allow_stretch: True
                keep_ratio: True
            Label:
                text: "Image jointe - ecrivez votre question puis Envoyer"
                font_size: "10sp"
                color: app.primary_color[0], app.primary_color[1], app.primary_color[2], 0.9
                halign: "left"
                valign: "middle"
                text_size: self.width, None
            Button:
                text: ""
                size_hint_x: None
                width: dp(36)
                bold: True
                background_normal: ""
                background_color: 0, 0, 0, 0
                color: 1, 0.3, 0.3, 1
                on_release: root.cancel_pending_image()
        BoxLayout:
            size_hint_y: None
            height: dp(16)
            Label:
                text: "DIRECT_INPUT_OVERRIDE"
                font_size: "10sp"
                bold: True
                color: app.primary_color[0], app.primary_color[1], app.primary_color[2], 0.85
                halign: "left"
                valign: "middle"
                text_size: self.size
            Label:
                id: input_status_label
                text: "AWAITING_COMMAND..."
                font_size: "10sp"
                color: 1, 0.7, 0.15, 0.85
                halign: "right"
                valign: "middle"
                text_size: self.size
        BoxLayout:
            spacing: dp(8)
            Button:
                text: "+"
                size_hint_x: None
                width: dp(46)
                bold: True
                font_size: "20sp"
                background_normal: ""
                background_color: 0, 0, 0, 0
                color: app.primary_color[0], app.primary_color[1], app.primary_color[2], 1
                canvas.before:
                    Color:
                        rgba: 0.03, 0.05, 0.07, 1
                    RoundedRectangle:
                        pos: self.pos
                        size: self.size
                        radius: [dp(6)]
                    Color:
                        rgba: app.primary_color[0], app.primary_color[1], app.primary_color[2], 0.9
                    Line:
                        rounded_rectangle: (self.x, self.y, self.width, self.height, dp(6))
                        width: 1.2
                on_release: root.pick_image()
            TextInput:
                id: msg_input
                hint_text: "ENTREZ VOTRE COMMANDE ICI..."
                multiline: False
                font_size: "14sp"
                background_color: 0, 0, 0, 0
                background_normal: ""
                background_active: ""
                foreground_color: 0.85, 1, 1, 1
                hint_text_color: 0.3, 0.55, 0.6, 1
                cursor_color: app.primary_color[0], app.primary_color[1], app.primary_color[2], 1
                padding: dp(10), dp(12)
                input_type: "text"
                keyboard_suggestions: False
                on_text_validate: root.send_message()
            Button:
                text: "ENVOYER"
                size_hint_x: None
                width: dp(130)
                bold: True
                font_size: "12sp"
                background_normal: ""
                background_color: 0, 0, 0, 0
                color: app.primary_color[0], app.primary_color[1], app.primary_color[2], 1
                canvas.before:
                    Color:
                        rgba: 0.03, 0.05, 0.07, 1
                    RoundedRectangle:
                        pos: self.pos
                        size: self.size
                        radius: [dp(6)]
                    Color:
                        rgba: app.primary_color[0], app.primary_color[1], app.primary_color[2], 0.9
                    Line:
                        rounded_rectangle: (self.x, self.y, self.width, self.height, dp(6))
                        width: 1.2
                on_release: root.send_message()
"""

BOOT_LINES = [
    "initialisation du noyau...",
    "chargement des modules ia...",
    "connexion aux serveurs...",
    "verification de securite...",
    "calibration vocale...",
    "systeme operationnel.",
]

class BootScreen(Screen):
    progress = NumericProperty(0)

    def on_enter(self):
        self._step = 0
        self._spin()
        Clock.schedule_once(self._next_line, 0.3)

    def _spin(self):
        ring = self.ids.arc_ring
        anim = Animation(angle=ring.angle + 360, duration=2.2, t="linear")
        anim.bind(on_complete=lambda *a: self._spin())
        anim.start(ring)

    def _next_line(self, dt):
        if self._step < len(BOOT_LINES):
            self.ids.boot_line.text = f"[color=00d9ff]>[/color] {BOOT_LINES[self._step]}"
            self._step += 1
            target = self._step / len(BOOT_LINES)
            Animation(progress=target, duration=0.5, t="out_quad").start(self)
            Clock.schedule_once(self._next_line, 0.55)
        else:
            Clock.schedule_once(self._finish, 0.5)

    def _finish(self, dt):
        app = App.get_running_app()
        app.sm.current = "chat"
        app.check_api_key()

class ChatScreen(Screen):
    pass

class LocationService:
    @staticmethod
    def request_permission():
        if not ANDROID:
            return
        try:
            from android.permissions import request_permissions, Permission
            request_permissions([
                Permission.ACCESS_FINE_LOCATION,
                Permission.ACCESS_COARSE_LOCATION,
            ])
        except Exception as e:
            print("location permission error:", e)

    @staticmethod
    def get_coordinates():
        if not ANDROID:
            return None
        try:
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            activity = PythonActivity.mActivity
            Context = autoclass("android.content.Context")
            location_manager = activity.getSystemService(Context.LOCATION_SERVICE)
            LocationManager = autoclass("android.location.LocationManager")
            providers = [LocationManager.NETWORK_PROVIDER, LocationManager.GPS_PROVIDER]
            for provider in providers:
                try:
                    location = location_manager.getLastKnownLocation(provider)
                    if location:
                        return location.getLatitude(), location.getLongitude()
                except Exception:
                    continue
            return None
        except Exception as e:
            print("get_coordinates error:", e)
            return None

class SearchService:
    @staticmethod
    def search(query, max_results=4):
        import urllib.parse
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
        html = https_get(url, timeout=10)
        titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', html, re.DOTALL)
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)

        def clean(text):
            text = re.sub(r'<[^>]+>', ' ', text)
            text = text.replace('&amp;', '&').replace('&#x27;', "'")
            text = text.replace('&quot;', '"')
            return text.strip()

        results = []
        for i in range(min(max_results, len(titles))):
            title = clean(titles[i])
            snippet = clean(snippets[i]) if i < len(snippets) else ""
            if title:
                results.append(f"- {title} : {snippet}")
        if not results:
            return None
        return "\n".join(results)

class WeatherService:
    WEATHER_CODES = {
        0: "ciel degage", 1: "plutot degage", 2: "partiellement nuageux",
        3: "couvert", 45: "brouillard", 48: "brouillard givrant",
        51: "bruine legere", 53: "bruine moderee", 55: "bruine dense",
        61: "pluie legere", 63: "pluie moderee", 65: "forte pluie",
        71: "neige legere", 73: "neige moderee", 75: "forte neige",
        80: "averses legeres", 81: "averses moderees", 82: "averses violentes",
        95: "orage", 96: "orage avec grele", 99: "orage violent avec grele",
    }

    @staticmethod
    def get_weather_by_city(city):
        import urllib.parse
        import json as json_module
        geo_url = (
            "https://geocoding-api.open-meteo.com/v1/search?name="
            + urllib.parse.quote(city)
            + "&count=1&language=fr&format=json"
        )
        geo_data = json_module.loads(https_get(geo_url))
        results = geo_data.get("results")
        if not results:
            return f"Ville '{city}' introuvable."
        place = results[0]
        lat, lon = place["latitude"], place["longitude"]
        real_name = place.get("name", city)
        country = place.get("country", "")
        return WeatherService._fetch_and_format(lat, lon, real_name, country)

    @staticmethod
    def get_weather_by_coordinates(lat, lon):
        return WeatherService._fetch_and_format(lat, lon, "votre position", "")

    @staticmethod
    def _fetch_and_format(lat, lon, place_name, country):
        import json as json_module
        weather_url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            "&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"
            "&timezone=auto"
        )
        weather_data = json_module.loads(https_get(weather_url))
        current = weather_data.get("current", {})
        temp = current.get("temperature_2m")
        humidity = current.get("relative_humidity_2m")
        wind = current.get("wind_speed_10m")
        code = current.get("weather_code")
        description = WeatherService.WEATHER_CODES.get(code, "conditions inconnues")
        return (
            f"Meteo a {place_name}{', ' + country if country else ''}:\n"
            f"{description}, {temp} °C\n"
            f"Humidite : {humidity}% - Vent : {wind} km/h"
        )

class SystemMonitor:
    @staticmethod
    def get_storage():
        try:
            import shutil
            total, used, free = shutil.disk_usage("/")
            to_go = lambda n: round(n / (1024 ** 3), 1)
            return f"{to_go(used)} Go / {to_go(total)} Go utilises"
        except Exception as e:
            return f"indisponible ({e})"

    @staticmethod
    def get_battery():
        if not ANDROID:
            return "indisponible (PC)"
        try:
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            activity = PythonActivity.mActivity
            Context = autoclass("android.content.Context")
            bm = activity.getSystemService(Context.BATTERY_SERVICE)
            level = bm.getIntProperty(4)
            return f"{level} %"
        except Exception as e:
            return f"indisponible ({e})"

    @staticmethod
    def get_cpu_temp():
        if not ANDROID:
            return "indisponible (PC)"
        try:
            with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                raw = f.read().strip()
            value = int(raw)
            temp_c = value / 1000 if value > 1000 else value
            return f"{temp_c:.1f} °C"
        except Exception:
            return "indisponible sur cet appareil"

class Orb(Widget):
    angle = NumericProperty(0)
    energy = NumericProperty(0.45)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        Clock.schedule_once(self._start_idle, 0.1)

    def _start_idle(self, dt):
        self._spin()
        self._breathe()

    def _spin(self):
        anim = Animation(angle=self.angle + 360, duration=6, t="linear")
        anim.bind(on_complete=lambda *a: self._spin())
        anim.start(self)

    def _breathe(self):
        anim = (
            Animation(energy=0.65, duration=1.6, t="in_out_sine")
            + Animation(energy=0.35, duration=1.6, t="in_out_sine")
        )
        anim.repeat = True
        anim.start(self)

    def pulse_speaking(self, text_len=50):
        try:
            duration = min(max(text_len * 0.04, 1.0), 6.0)
            anim = Animation(energy=0.95, duration=0.25) + Animation(energy=0.45, duration=duration)
            anim.start(self)
        except Exception as e:
            print("pulse_speaking error:", e)

class RootWidget(BoxLayout):
    pending_image_path = StringProperty("")

    def on_kv_post(self, base_widget):
        self._start_pulse()
        self._update_clock()
        Clock.schedule_interval(lambda dt: self._update_clock(), 1)
        self._register_activity_result_listener()

    def _update_clock(self):
        try:
            now = datetime.now()
            self.ids.clock_label.text = now.strftime("%H:%M:%S")
            self.ids.date_label.text = now.strftime("%d/%m/%Y")
        except Exception as e:
            print("clock update error:", e)

    def _start_pulse(self):
        dot = self.ids.status_label
        anim = Animation(opacity=0.4, duration=0.9) + Animation(opacity=1, duration=0.9)
        anim.repeat = True
        anim.start(dot)

    def add_message(self, text, is_user=True):
        if is_user:
            hexcolor = "ff8c1a"
        else:
            r, g, b = App.get_running_app().primary_color
            hexcolor = "%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))
        sender = "TOI" if is_user else "JARVIS"
        bubble_outer = self._build_bubble(text, sender, hexcolor, is_user)
        self.ids.chat_box.add_widget(bubble_outer)
        Clock.schedule_once(lambda dt: self.scroll_to_bottom(), 0.1)
        return bubble_outer

    def _build_bubble(self, text, sender, hexcolor, is_user):
        from kivy.factory import Factory
        outer = BoxLayout(size_hint_y=None, padding=(dp(4), dp(2)))
        bubble = Factory.ChatBubble(orientation="vertical", padding=(dp(14), dp(10)))
        bubble.size_hint_x = 0.82
        if is_user:
            bubble.bg_color = (1, 0.55, 0.1, 0.10)
            bubble.line_color = (1, 0.55, 0.1, 0.6)
        else:
            r, g, b = App.get_running_app().primary_color
            bubble.bg_color = (r, g, b, 0.10)
            bubble.line_color = (r, g, b, 0.6)

        label_sender = Label(
            text=f"[b][color={hexcolor}]{sender}[/color][/b]",
            markup=True,
            size_hint_y=None,
            height=dp(18),
            font_size="11sp",
            halign="left",
            valign="middle",
        )
        label_sender.bind(width=lambda i, v: i.setter("text_size")(i, (v, None)))

        label_text = Label(
            text=text,
            size_hint_y=None,
            color=(0.92, 0.97, 1, 1),
            font_size="14sp",
            halign="left",
            valign="top",
        )
        label_text.bind(width=lambda i, v: i.setter("text_size")(i, (v, None)))
        label_text.bind(texture_size=lambda i, v: i.setter("height")(i, v[1]))

        bubble.add_widget(label_sender)
        bubble.add_widget(label_text)
        bubble.bind(minimum_height=lambda i, v: i.setter("height")(i, v))

        if is_user:
            outer.add_widget(BoxLayout())
            outer.add_widget(bubble)
        else:
            outer.add_widget(bubble)
            outer.add_widget(BoxLayout())

        outer.bind(minimum_height=lambda i, v: i.setter("height")(i, v))
        bubble.bind(height=lambda i, v: setattr(outer, "height", v + dp(4)))
        return outer

    def scroll_to_bottom(self):
        self.ids.scroll.scroll_y = 0

    def pick_image(self):
        if not ANDROID:
            try:
                from plyer import filechooser
                filechooser.open_file(
                    on_selection=self._on_image_selected,
                    filters=[("Images", "*.jpg", "*.jpeg", "*.png")],
                )
            except Exception as e:
                self.add_message(f"Impossible d'ouvrir le selecteur d'image : {e}", is_user=False)
            return
        try:
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            Intent = autoclass("android.content.Intent")
            activity = PythonActivity.mActivity
            intent = Intent(Intent.ACTION_GET_CONTENT)
            intent.setType("image/*")
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            REQUEST_CODE = 9001
            self._register_activity_result_listener()
            activity.startActivityForResult(intent, REQUEST_CODE)
        except Exception as e:
            self.add_message(f"Impossible d'ouvrir le selecteur d'image (natif) : {e}", is_user=False)

    def _register_activity_result_listener(self):
        if getattr(self, "_activity_listener_registered", False):
            return
        try:
            from android import activity as android_activity
            android_activity.bind(on_activity_result=self._on_activity_result)
            self._activity_listener_registered = True
        except Exception as e:
            print("register_activity_result_listener error:", e)

    def _on_activity_result(self, request_code, result_code, intent):
        if request_code != 9001:
            return
        try:
            Activity = autoclass("android.app.Activity")
            if result_code != Activity.RESULT_OK or intent is None:
                Clock.schedule_once(lambda dt: self.add_message("Selection d'image annulee.", is_user=False))
                return
            uri = intent.getData()
            if uri is None:
                Clock.schedule_once(lambda dt: self.add_message("[DIAG] aucune URI recue.", is_user=False))
                return
            image_path = self._copy_uri_to_local_file(uri)
            if image_path:
                Clock.schedule_once(lambda dt: setattr(self, "pending_image_path", image_path))
        except Exception as e:
            Clock.schedule_once(lambda dt: self.add_message(f"[DIAG] erreur image: {str(e)[-200:]}", is_user=False))

    def _copy_uri_to_local_file(self, uri):
        try:
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            activity = PythonActivity.mActivity
            content_resolver = activity.getContentResolver()
            input_stream = content_resolver.openInputStream(uri)
            import time
            app_dir = App.get_running_app().user_data_dir
            dest_path = os.path.join(app_dir, f"picked_image_{int(time.time())}.jpg")
            
            # Utiliser Java NIO channels pour copier sans allouer de buffer Pyjnius modifiable
            Channels = autoclass("java.nio.channels.Channels")
            ReadableByteChannel = Channels.newChannel(input_stream)
            FileOutputStream = autoclass("java.io.FileOutputStream")
            output_stream = FileOutputStream(dest_path)
            FileChannel = output_stream.getChannel()
            
            # Copier jusqu'à 100 Mo
            FileChannel.transferFrom(ReadableByteChannel, 0, 104857600)
            
            ReadableByteChannel.close()
            FileChannel.close()
            output_stream.close()
            input_stream.close()
            return dest_path
        except Exception as e:
            print("_copy_uri_to_local_file error:", e)
            return None

    def _on_image_selected(self, selection):
        def diag(msg):
            Clock.schedule_once(lambda dt: self.add_message(msg, is_user=False))
        if not selection:
            diag("[DIAG] selection vide")
            return
        image_path = selection if isinstance(selection, str) else (selection[0] if selection else None)
        if not image_path or not os.path.exists(image_path):
            diag(f"[DIAG] fichier introuvable: {image_path}")
            return
        diag(f"[DIAG] image OK: {image_path}")
        Clock.schedule_once(lambda dt: setattr(self, "pending_image_path", image_path))

    def cancel_pending_image(self):
        self.pending_image_path = ""

    def get_image_analysis(self, image_path, question):
        app = App.get_running_app()
        try:
            import base64
            with open(image_path, "rb") as f:
                image_data = base64.b64encode(f.read()).decode("utf-8")
            ext = image_path.lower().rsplit(".", 1)[-1]
            mime = "image/png" if ext == "png" else "image/jpeg"
            response = app.client.chat.completions.create(
                model="llama-3.2-11b-vision-preview",
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": question.strip() or "Decris cette image."},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_data}"}},
                ]}],
            )
            reply = response.choices[0].message.content
        except Exception as e:
            reply = f"Impossible d'analyser l'image : {e}"
        Clock.schedule_once(lambda dt: self.replace_last_bubble(reply))

    def send_message(self):
        text = self.ids.msg_input.text.strip()
        if self.pending_image_path:
            image_path = self.pending_image_path
            self.pending_image_path = ""
            self.ids.msg_input.text = ""
            display_text = text if text else "[Image envoyee]"
            self.add_message(display_text, is_user=True)
            App.get_running_app().add_message_to_history(display_text, is_user=True)
            self.set_status("analyse de l'image en cours...", busy=True)
            self.add_message("...", is_user=False)
            threading.Thread(target=self.get_image_analysis, args=(image_path, text), daemon=True).start()
            return
        if not text:
            return
        self.ids.msg_input.text = ""
        self.add_message(text, is_user=True)
        App.get_running_app().add_message_to_history(text, is_user=True)
        if text.lower() == "quit":
            App.get_running_app().stop()
            return

        shopping_item = ShoppingListDetector.parse_shopping_item(text)
        if shopping_item:
            App.get_running_app().add_to_shopping_list(shopping_item)
            self.set_status("article ajoute...", busy=True)
            self.add_message(f" '{shopping_item}' ajouté à la liste de courses.", is_user=False)
            return

        appointment = AppointmentDetector.parse_appointment(text)
        if appointment:
            formatted = appointment["formatted"]
            title = appointment["title"]
            target_dt = appointment["datetime"]
            
            NotificationService.schedule_reminder(title, target_dt)
            App.get_running_app().save_appointment(title, target_dt)
            
            self.set_status("rappel programme...", busy=True)
            self.add_message(
                f" Rendez-vous '{title}' confirmé pour le {formatted}.\n"
                f"Je vous notifierai à :\n"
                f"• 24h avant\n• 12h avant\n• 1h avant\n• 45 min avant\n• 30 min avant\n• 15 min avant",
                is_user=False
            )
            return

        self.set_status("traitement en cours...", busy=True)
        self.add_message("...", is_user=False)
        if self._is_weather_request(text):
            threading.Thread(target=self.get_weather_response, args=(text,), daemon=True).start()
        else:
            threading.Thread(target=self.get_ai_response, args=(text,), daemon=True).start()

    def _is_weather_request(self, text):
        text_low = text.lower()
        return any(k in text_low for k in ["meteo", "météo", "quel temps", "il fait quel temps", "weather"])

    def _is_search_request(self, text):
        text_low = text.lower()
        keywords = ["recherche sur internet", "recherche sur google", "cherche sur internet",
                     "cherche sur google", "fait une recherche", "recherche web",
                     "google ça", "google ca", "cherche sur le web"]
        return any(k in text_low for k in keywords)

    def _extract_city_from_text(self, text):
        # 1. Essayer avec la casse d'origine (capitalisée) pour être précis (avec \b corrigé)
        match = re.search(r"\b(?:a|à|de)\s+([A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜŸ][\wÀÂÄÉÈÊËÎÔÖÙÛÜŸ\-]+(?:\s+[A-ZÀÂÄÉÈÊËÎÏÔÖÙÛÜŸ][\wÀÂÄÉÈÊËÎÏÔÖÙÛÜŸ\-]+)*)", text)
        if match:
            return match.group(1).strip()
        # 2. Fallback pour les minuscules
        match = re.search(r"\b(?:a|à|de)\s+([a-zA-ZÀ-ÿ\-]+(?:\s+[a-zA-ZÀ-ÿ\-]+)*)", text)
        if match:
            candidate = match.group(1).strip()
            first_word = candidate.split()[0].lower()
            if (first_word not in ["la", "le", "les", "mon", "ma", "mes", "ce", "cet", "cette", "ces", "un", "une", "des"]) and not (first_word.startswith("l'") or first_word.startswith("d'")):
                return candidate
        return None

    def get_weather_response(self, text):
        app = App.get_running_app()
        city = self._extract_city_from_text(text) or app.default_city
        try:
            if city:
                reply = WeatherService.get_weather_by_city(city)
            else:
                coords = LocationService.get_coordinates()
                if coords:
                    reply = WeatherService.get_weather_by_coordinates(*coords)
                else:
                    reply = ("Je n'ai pas de ville par defaut. Definissez-en une dans Menu > Parametres "
                             "ou demandez 'meteo a Paris'.")
        except Exception as e:
            reply = f"Impossible de recuperer la meteo : {e}"
        Clock.schedule_once(lambda dt: self.replace_last_bubble(reply))

    def set_status(self, text, busy=False):
        if busy:
            color = "ff8c1a"
        else:
            r, g, b = App.get_running_app().primary_color
            color = "%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))
        self.ids.status_label.text = f"[color={color}]\u25CF {text}[/color]"
        try:
            self.ids.input_status_label.text = "PROCESSING..." if busy else "AWAITING_COMMAND..."
        except Exception:
            pass

    def _section_label(self, text, color):
        return Label(text=f"[b]{text}[/b]", markup=True, size_hint_y=None, height=dp(22),
                     font_size="11sp", color=color, halign="left", valign="middle")

    def _menu_button(self, text, callback, rgb=None, icon=""):
        if rgb is None:
            rgb = App.get_running_app().primary_color
        from kivy.factory import Factory
        btn = Factory.PillCapsule(text=f"{icon} {text}", size_hint_y=None, height=dp(44),
                                   bold=True, font_size="12sp", color=(rgb[0], rgb[1], rgb[2], 1))
        btn.line_color = (rgb[0], rgb[1], rgb[2], 0.85)
        btn.fill_color = (0.05, 0.08, 0.1, 1)
        def on_press(inst):
            if hasattr(self, "_menu_popup"):
                self._menu_popup.dismiss()
            callback()
        btn.bind(on_release=on_press)
        return btn

    def open_history(self):
        app = App.get_running_app()
        primary = (app.primary_color[0], app.primary_color[1], app.primary_color[2], 1)
        layout = BoxLayout(orientation="vertical", spacing=dp(10), padding=dp(16))
        header = BoxLayout(size_hint_y=None, height=dp(30))
        header.add_widget(Label(text="[b]CONVERSATIONS[/b]", markup=True, color=primary,
                                 font_size="13sp", halign="left", valign="middle"))
        close_x = Button(text="[ X ]", markup=True, size_hint=(None, None), size=(dp(50), dp(28)),
                          background_normal="", background_color=(0, 0, 0, 0), color=(0.7, 0.8, 0.85, 1))
        header.add_widget(close_x)
        layout.add_widget(header)
        from kivy.factory import Factory
        new_conv_btn = Factory.PillCapsule(text="NOUVELLE CONVERSATION", size_hint_y=None,
                                            height=dp(44), bold=True, font_size="12sp", color=primary)
        new_conv_btn.line_color = primary
        layout.add_widget(new_conv_btn)
        scroll = ScrollView(size_hint_y=1)
        history_box = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(8))
        history_box.bind(minimum_height=history_box.setter("height"))
        conversations = app.load_conversations()
        if not conversations:
            history_box.add_widget(Label(text="Aucune conversation.", font_size="11sp",
                                          size_hint_y=None, height=dp(30), color=(0.5, 0.6, 0.65, 1)))
        else:
            for conv in conversations:
                is_current = conv.get("id") == app.current_conversation_id
                conv_btn = Factory.BorderedField(text=conv.get("title", "Sans titre"),
                                                  size_hint_y=None, height=dp(44), font_size="12sp",
                                                  color=primary if is_current else (0.8, 0.9, 0.95, 1))
                conv_btn.line_color = primary if is_current else (0.4, 0.5, 0.55, 0.6)
                conv_id = conv.get("id")
                conv_btn.bind(on_release=lambda inst, cid=conv_id: self._open_conversation(cid))
                history_box.add_widget(conv_btn)
        scroll.add_widget(history_box)
        layout.add_widget(scroll)
        popup = Popup(title="", content=layout, size_hint=(0.85, 0.85), auto_dismiss=False,
                       background_color=(0.02, 0.05, 0.07, 0.97))
        close_x.bind(on_release=lambda inst: popup.dismiss())
        new_conv_btn.bind(on_release=lambda inst: (popup.dismiss(), self._new_conversation()))
        self._history_popup = popup
        popup.open()

    def _new_conversation(self):
        app = App.get_running_app()
        app.start_new_conversation()
        self.ids.chat_box.clear_widgets()
        self.add_message("Bonjour. Tous les systemes sont operationnels. Comment puis-je vous etre utile ?", is_user=False)

    def _open_conversation(self, conversation_id):
        if hasattr(self, "_history_popup"):
            self._history_popup.dismiss()
        app = App.get_running_app()
        conversations = app.load_conversations()
        target = next((c for c in conversations if c.get("id") == conversation_id), None)
        if not target:
            return
        app.current_conversation_id = target["id"]
        app.current_conversation_title = target.get("title")
        app.current_conversation_messages = list(target.get("messages", []))
        self.ids.chat_box.clear_widgets()
        for msg in app.current_conversation_messages:
            self.add_message(msg.get("text", ""), is_user=msg.get("is_user", False))

    def open_menu(self):
        app = App.get_running_app()
        layout = BoxLayout(orientation="vertical", spacing=dp(10), padding=dp(18))
        header = BoxLayout(size_hint_y=None, height=dp(30))
        header.add_widget(Label(text="[b]CONTROLES_SYSTEME // MENU[/b]", markup=True,
                                 color=(app.primary_color[0], app.primary_color[1], app.primary_color[2], 1),
                                 font_size="13sp", halign="left", valign="middle"))
        close_x = Button(text="[ X ]", markup=True, size_hint=(None, None), size=(dp(50), dp(28)),
                          background_normal="", background_color=(0, 0, 0, 0), color=(0.7, 0.8, 0.85, 1))
        header.add_widget(close_x)
        layout.add_widget(header)

        layout.add_widget(self._section_label("PARAMETRES", (0.65, 0.45, 0.95, 1)))
        layout.add_widget(self._menu_button("PARAMETRES", self.open_settings, (0.65, 0.45, 0.95)))

        appointments = app.load_appointments()
        rdv_count = len(appointments)
        rdv_label = f"QUOTIDIEN ({rdv_count})" if rdv_count > 0 else "QUOTIDIEN"
        layout.add_widget(self._section_label(rdv_label, (0.15, 0.9, 0.4)))
        layout.add_widget(self._menu_button("VOIR MES RDV", self.open_appointments_menu, (0.15, 0.9, 0.4), icon=""))
        layout.add_widget(self._menu_button("VOIR MA LISTE", self.open_shopping_list_menu, (0.15, 0.9, 0.4), icon=""))
        
        layout.add_widget(self._section_label("OUTILS SYSTEME", (1, 0.55, 0.1, 1)))
        layout.add_widget(self._menu_button("VERIFIER MISES A JOUR", self.check_for_update, (1, 0.55, 0.1)))
        layout.add_widget(self._menu_button("SYSTEM MONITOR", self.open_system_monitor, (1, 0.55, 0.1)))

        layout.add_widget(BoxLayout(size_hint_y=1))
        layout.add_widget(Label(text=f"VERSION {APP_VERSION}", font_size="10sp", size_hint_y=None,
                                 height=dp(20), color=(app.primary_color[0], app.primary_color[1], app.primary_color[2], 0.6)))
        popup = Popup(title="", content=layout, size_hint=(0.85, 0.75), auto_dismiss=False,
                       background_color=(0.02, 0.04, 0.06, 0.97))
        close_x.bind(on_release=lambda inst: popup.dismiss())
        self._menu_popup = popup
        popup.open()

    def open_appointments_menu(self):
        app = App.get_running_app()
        layout = BoxLayout(orientation="vertical", spacing=dp(10), padding=dp(18))
        
        header = BoxLayout(size_hint_y=None, height=dp(30))
        header.add_widget(Label(text="[b] MES RENDEZ-VOUS[/b]", markup=True,
                                 color=(1, 0.7, 0.15, 1), font_size="13sp", halign="left", valign="middle"))
        close_x = Button(text="[ X ]", markup=True, size_hint=(None, None), size=(dp(50), dp(28)),
                          background_normal="", background_color=(0, 0, 0, 0), color=(0.7, 0.8, 0.85, 1))
        header.add_widget(close_x)
        layout.add_widget(header)

        scroll = ScrollView(size_hint_y=1)
        rdv_box = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(8))
        rdv_box.bind(minimum_height=rdv_box.setter("height"))

        appointments = app.load_appointments()
        if appointments:
            for appt in appointments:
                container = self._appointment_row(appt)
                rdv_box.add_widget(container)
        else:
            rdv_box.add_widget(Label(text="Aucun rendez-vous programme.", font_size="11sp",
                                      size_hint_y=None, height=dp(30), color=(0.5, 0.6, 0.65, 1)))

        scroll.add_widget(rdv_box)
        layout.add_widget(scroll)

        popup = Popup(title="", content=layout, size_hint=(0.85, 0.7), auto_dismiss=False,
                       background_color=(0.02, 0.04, 0.06, 0.97))
        close_x.bind(on_release=lambda inst: popup.dismiss())
        self._appointments_popup = popup
        popup.open()

    def _appointment_row(self, appt):
        from kivy.factory import Factory
        target_dt = datetime.fromisoformat(appt["datetime"])
        now = datetime.now()
        remaining = target_dt - now

        if remaining.total_seconds() <= 0:
            time_str = "[color=ff4444]EXPIRE[/color]"
        elif remaining.days > 0:
            time_str = f"[color=ffcc00]dans {remaining.days}j {remaining.seconds//3600}h[/color]"
        else:
            hours = remaining.seconds // 3600
            minutes = (remaining.seconds % 3600) // 60
            time_str = f"[color=00ff88]dans {hours}h{minutes:02d}min[/color]"

        title = appt.get("title", "RDV")
        formatted = target_dt.strftime("%d/%m à %H:%M")

        btn = Factory.BorderedField(
            text=f"[b]{title}[/b]\\n[size=10sp]{formatted} • {time_str}[/size]",
            markup=True,
            size_hint_y=None,
            height=dp(52),
            font_size="12sp",
            color=(1, 0.85, 0.3, 1),
        )
        btn.line_color = (1, 0.7, 0.15, 0.6)
        btn.fill_color = (0.08, 0.06, 0.02, 1)

        delete_btn = Button(
            text="✕",
            size_hint=(None, None),
            size=(dp(30), dp(30)),
            pos_hint={"right": 1, "center_y": 0.5},
            background_normal="",
            background_color=(0, 0, 0, 0),
            color=(1, 0.3, 0.3, 0.8),
            font_size="14sp",
        )
        appt_id = appt.get("id", "")
        delete_btn.bind(on_release=lambda inst, aid=appt_id: self._delete_appointment(aid))

        container = BoxLayout(size_hint_y=None, height=dp(52))
        container.add_widget(btn)
        container.add_widget(delete_btn)
        return container

    def _delete_appointment(self, appt_id):
        app = App.get_running_app()
        app.delete_appointment(appt_id)
        if hasattr(self, "_appointments_popup"):
            self._appointments_popup.dismiss()
        self.open_appointments_menu()

    def open_shopping_list_menu(self):
        app = App.get_running_app()
        layout = BoxLayout(orientation="vertical", spacing=dp(10), padding=dp(18))
        
        header = BoxLayout(size_hint_y=None, height=dp(30))
        header.add_widget(Label(text="[b] LISTE DE COURSES[/b]", markup=True,
                                 color=(0.15, 0.9, 0.4, 1), font_size="13sp", halign="left", valign="middle"))
        close_x = Button(text="[ X ]", markup=True, size_hint=(None, None), size=(dp(50), dp(28)),
                          background_normal="", background_color=(0, 0, 0, 0), color=(0.7, 0.8, 0.85, 1))
        header.add_widget(close_x)
        layout.add_widget(header)

        scroll = ScrollView(size_hint_y=1)
        shop_box = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(8))
        shop_box.bind(minimum_height=shop_box.setter("height"))

        shopping_list = app.load_shopping_list()
        if shopping_list:
            for item in shopping_list:
                row = self._shopping_item_row(item)
                shop_box.add_widget(row)
        else:
            shop_box.add_widget(Label(text="Votre liste est vide. Dites 'ajoute X à la liste'.",
                                       font_size="11sp", size_hint_y=None, height=dp(30),
                                       color=(0.5, 0.6, 0.65, 1)))

        scroll.add_widget(shop_box)
        layout.add_widget(scroll)

        popup = Popup(title="", content=layout, size_hint=(0.85, 0.7), auto_dismiss=False,
                       background_color=(0.02, 0.04, 0.06, 0.97))
        close_x.bind(on_release=lambda inst: popup.dismiss())
        self._shopping_popup = popup
        popup.open()

    def _shopping_item_row(self, item):
        from kivy.factory import Factory
        btn = Factory.BorderedField(
            text=f"[b]• {item}[/b]",
            markup=True,
            size_hint_y=None,
            height=dp(44),
            font_size="13sp",
            color=(0.15, 0.9, 0.4, 1),
        )
        btn.line_color = (0.15, 0.9, 0.4, 0.5)
        btn.fill_color = (0.02, 0.08, 0.02, 1)

        delete_btn = Button(
            text="✕",
            size_hint=(None, None),
            size=(dp(30), dp(30)),
            pos_hint={"right": 1, "center_y": 0.5},
            background_normal="",
            background_color=(0, 0, 0, 0),
            color=(1, 0.3, 0.3, 0.8),
            font_size="14sp",
        )
        delete_btn.bind(on_release=lambda inst, it=item: self._delete_shopping_item(it))

        container = BoxLayout(size_hint_y=None, height=dp(44))
        container.add_widget(btn)
        container.add_widget(delete_btn)
        return container

    def _delete_shopping_item(self, item):
        app = App.get_running_app()
        app.remove_from_shopping_list(item)
        if hasattr(self, "_shopping_popup"):
            self._shopping_popup.dismiss()
        self.open_shopping_list_menu()

    def open_system_monitor(self):
        if hasattr(self, "_menu_popup"):
            self._menu_popup.dismiss()
        layout = BoxLayout(orientation="vertical", spacing=dp(14), padding=dp(20))
        layout.add_widget(Label(text="[b]SYSTEM MONITOR[/b]", markup=True, size_hint_y=None,
                                 height=dp(28), color=(1, 0.55, 0.1, 1), font_size="14sp"))
        info_rows = [("STOCKAGE", SystemMonitor.get_storage()),
                      ("BATTERIE", SystemMonitor.get_battery()),
                      ("TEMPERATURE CPU", SystemMonitor.get_cpu_temp())]
        app = App.get_running_app()
        for label_text, value_text in info_rows:
            row = BoxLayout(size_hint_y=None, height=dp(30))
            row.add_widget(Label(text=label_text, font_size="11sp", color=(0.6, 0.85, 0.9, 1),
                                  halign="left", valign="middle", text_size=(dp(140), dp(30)),
                                  size_hint_x=None, width=dp(140)))
            row.add_widget(Label(text=value_text, font_size="12sp", bold=True,
                                  color=(app.primary_color[0], app.primary_color[1], app.primary_color[2], 1),
                                  halign="left", valign="middle", text_size=(dp(160), dp(30))))
            layout.add_widget(row)
        close_btn = Button(text="FERMER", size_hint_y=None, height=dp(44), bold=True,
                            background_normal="", background_color=(0.12, 0.12, 0.15, 1),
                            color=(0.8, 0.9, 0.95, 1))
        layout.add_widget(close_btn)
        popup = Popup(title="X", content=layout, size_hint=(0.85, 0.55), auto_dismiss=False,
                       background_color=(0.02, 0.04, 0.06, 0.97))
        close_btn.bind(on_release=lambda inst: popup.dismiss())
        popup.open()

    def open_settings(self):
        app = App.get_running_app()
        primary = (app.primary_color[0], app.primary_color[1], app.primary_color[2], 1)
        layout = BoxLayout(orientation="vertical", spacing=dp(14), padding=dp(20))
        header = BoxLayout(size_hint_y=None, height=dp(30))
        header.add_widget(Label(text="[b]CONFIGURATION PROFIL[/b]", markup=True, color=primary,
                                 font_size="14sp", halign="left", valign="middle"))
        close_x = Button(text="[ X ]", markup=True, size_hint=(None, None), size=(dp(50), dp(28)),
                          background_normal="", background_color=(0, 0, 0, 0), color=(0.7, 0.8, 0.85, 1))
        header.add_widget(close_x)
        layout.add_widget(header)
        layout.add_widget(Label(text="LANGUE DE REPONSE", font_size="10sp", bold=True,
                                 size_hint_y=None, height=dp(20), color=primary, halign="left"))
        from kivy.factory import Factory
        lang_field = Factory.BorderedField(text=f"{app.language}    ", size_hint_y=None,
                                            height=dp(42), font_size="13sp", color=(0.85, 1, 1, 1))
        lang_field.line_color = primary
        lang_field.bind(on_release=lambda inst: self._cycle_language())
        layout.add_widget(lang_field)
        layout.add_widget(Label(text="THEME DE COULEUR", font_size="10sp", bold=True,
                                 size_hint_y=None, height=dp(20), color=primary, halign="left"))
        theme_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        for name, rgb in THEMES.items():
            swatch = Button(text="", background_normal="", background_color=(rgb[0], rgb[1], rgb[2], 1))
            if name == app.theme_name:
                swatch.text = "[b]OK[/b]"
                swatch.markup = True
                swatch.color = (0, 0, 0, 1)
            swatch.bind(on_release=lambda inst, n=name: self._select_theme(n))
            theme_row.add_widget(swatch)
        layout.add_widget(theme_row)
        layout.add_widget(Label(text="VILLE PAR DEFAUT (METEO)", font_size="10sp", bold=True,
                                 size_hint_y=None, height=dp(20), color=primary, halign="left"))
        city_value = app.default_city or "Non definie"
        city_field = Factory.BorderedField(text=f"{city_value}    ", size_hint_y=None,
                                            height=dp(42), font_size="13sp", color=(0.85, 1, 1, 1))
        city_field.line_color = primary
        city_field.bind(on_release=lambda inst: self._edit_city())
        layout.add_widget(city_field)
        layout.add_widget(Label(text="CLE API GROQ", font_size="10sp", bold=True,
                                 size_hint_y=None, height=dp(20), color=(1, 0.35, 0.35, 1), halign="left"))
        saved_key = app.get_saved_key() or ""
        key_display = (saved_key[:4] + "****" + saved_key[-4:]) if len(saved_key) > 8 else ("****" if saved_key else "Non definie")
        key_field = Factory.BorderedField(text=f"{key_display}    ", size_hint_y=None,
                                           height=dp(42), font_size="13sp", color=(1, 0.7, 0.7, 1))
        key_field.line_color = (1, 0.35, 0.35, 0.8)
        key_field.bind(on_release=lambda inst: self._edit_api_key())
        layout.add_widget(key_field)
        self._settings_popup = Popup(title="", content=layout, size_hint=(0.88, 0.85),
                                      auto_dismiss=False, background_color=(0.02, 0.05, 0.07, 0.97))
        close_x.bind(on_release=lambda inst: self._settings_popup.dismiss())
        self._settings_popup.open()

    def _cycle_language(self):
        app = App.get_running_app()
        idx = LANGUAGES.index(app.language) if app.language in LANGUAGES else 0
        self._select_language(LANGUAGES[(idx + 1) % len(LANGUAGES)])

    def _select_language(self, lang):
        App.get_running_app().set_language(lang)
        if hasattr(self, "_settings_popup"):
            self._settings_popup.dismiss()
        self.open_settings()

    def _select_theme(self, theme_name):
        App.get_running_app().set_theme(theme_name)
        if hasattr(self, "_settings_popup"):
            self._settings_popup.dismiss()
        self.open_settings()

    def _edit_city(self):
        if hasattr(self, "_settings_popup"):
            self._settings_popup.dismiss()
        app = App.get_running_app()
        primary = (app.primary_color[0], app.primary_color[1], app.primary_color[2], 1)
        layout = BoxLayout(orientation="vertical", spacing=dp(14), padding=dp(20))
        layout.add_widget(Label(text="Entrez le nom de votre ville :", font_size="12sp",
                                 size_hint_y=None, height=dp(24), color=primary))
        city_input = TextInput(text=app.default_city or "", multiline=False,
                                size_hint_y=None, height=dp(42), font_size="14sp")
        layout.add_widget(city_input)
        from kivy.factory import Factory
        validate_btn = Factory.PillCapsule(text="VALIDER", size_hint_y=None, height=dp(44),
                                            bold=True, color=primary)
        validate_btn.line_color = primary
        layout.add_widget(validate_btn)
        popup = Popup(title="", content=layout, size_hint=(0.85, 0.35), auto_dismiss=False,
                       background_color=(0.02, 0.05, 0.07, 0.97))
        def validate(inst):
            app.set_default_city(city_input.text.strip())
            popup.dismiss()
            if hasattr(self, "_settings_popup"):
                self._settings_popup.dismiss()
            self.open_settings()
        validate_btn.bind(on_release=validate)
        popup.open()

    def _edit_api_key(self):
        if hasattr(self, "_settings_popup"):
            self._settings_popup.dismiss()
        app = App.get_running_app()
        layout = BoxLayout(orientation="vertical", spacing=dp(14), padding=dp(20))
        layout.add_widget(Label(text="[b][color=ff5555]CLE API GROQ[/color][/b]\nCollez votre nouvelle cle Groq ici :",
                                 markup=True, font_size="12sp", size_hint_y=None, height=dp(48),
                                 halign="center", color=(0.85, 1, 1, 1)))
        key_input = TextInput(
            text=app.get_saved_key() or "",
            multiline=False,
            size_hint_y=None,
            height=dp(42),
            font_size="12sp",
            password=False,
            foreground_color=(0.85, 1, 1, 1),
            background_color=(0.04, 0.08, 0.1, 1),
        )
        layout.add_widget(key_input)
        from kivy.factory import Factory
        validate_btn = Factory.PillCapsule(text="SAUVEGARDER ET APPLIQUER", size_hint_y=None,
                                            height=dp(44), bold=True, color=(1, 0.35, 0.35, 1))
        validate_btn.line_color = (1, 0.35, 0.35, 0.85)
        validate_btn.fill_color = (0.1, 0.02, 0.02, 1)
        layout.add_widget(validate_btn)
        cancel_btn = Factory.PillCapsule(text="ANNULER", size_hint_y=None, height=dp(38),
                                          bold=True, color=(0.5, 0.7, 0.8, 1))
        cancel_btn.line_color = (0.5, 0.7, 0.8, 0.5)
        layout.add_widget(cancel_btn)
        popup = Popup(title="", content=layout, size_hint=(0.88, 0.45), auto_dismiss=False,
                       background_color=(0.02, 0.05, 0.07, 0.97))
        def validate(inst):
            new_key = key_input.text.strip()
            if new_key:
                app.save_key(new_key)
                app.init_client(new_key)
                popup.dismiss()
                self.open_settings()
                self.set_status("cle API mise a jour", busy=False)
            else:
                key_input.hint_text = "La cle ne peut pas etre vide !"
        def cancel(inst):
            popup.dismiss()
            self.open_settings()
        validate_btn.bind(on_release=validate)
        cancel_btn.bind(on_release=cancel)
        popup.open()

    def check_for_update(self):
        self.set_status("verification des mises a jour...", busy=True)
        threading.Thread(target=self._check_update_thread, daemon=True).start()

    def _check_update_thread(self):
        try:
            latest_version = https_get(VERSION_CHECK_URL).strip()
            if latest_version != APP_VERSION:
                Clock.schedule_once(lambda dt: self._show_update_popup(latest_version))
            else:
                Clock.schedule_once(lambda dt: self.set_status("vous avez la derniere version", busy=False))
        except Exception as e:
            Clock.schedule_once(lambda dt: self.set_status(f"verification impossible : {e}", busy=False))

    def _show_update_popup(self, latest_version):
        self.set_status(f"nouvelle version disponible : {latest_version}", busy=True)
        layout = BoxLayout(orientation="vertical", spacing=dp(12), padding=dp(20))
        layout.add_widget(Label(text=(f"[b][color=ff8c1a]MISE A JOUR DISPONIBLE[/color][/b]\n"
                                       f"Version actuelle : {APP_VERSION}\n"
                                       f"Nouvelle version : {latest_version}"),
                                 markup=True, halign="center"))
        btn = Button(text="TELECHARGER LA MISE A JOUR", size_hint_y=None, height=dp(48), bold=True,
                      background_normal="", background_color=(1, 0.7, 0.15, 1), color=(0.05, 0.05, 0.05, 1))
        layout.add_widget(btn)
        close_btn = Button(text="Plus tard", size_hint_y=None, height=dp(36), background_normal="",
                            background_color=(0.15, 0.15, 0.18, 1), color=(0.7, 0.8, 0.85, 1))
        layout.add_widget(close_btn)
        popup = Popup(title="", content=layout, size_hint=(0.88, 0.45), auto_dismiss=False,
                       background_color=(0.02, 0.05, 0.07, 0.97))
        def open_download(instance):
            try:
                Intent = autoclass("android.content.Intent")
                Uri = autoclass("android.net.Uri")
                PythonActivity = autoclass("org.kivy.android.PythonActivity")
                activity = PythonActivity.mActivity
                browser_intent = Intent(Intent.ACTION_VIEW, Uri.parse(APK_DOWNLOAD_URL))
                activity.startActivity(browser_intent)
            except Exception as e:
                print("open download error:", e)
            popup.dismiss()
            self.set_status("systeme en ligne", busy=False)
        def close_popup(instance):
            popup.dismiss()
            self.set_status("systeme en ligne", busy=False)
        btn.bind(on_release=open_download)
        close_btn.bind(on_release=close_popup)
        popup.open()

    def get_ai_response(self, text):
        app = App.get_running_app()
        try:
            system_msg = {"role": "system",
                           "content": f"Tu es Jarvis, un assistant IA. Reponds TOUJOURS en {app.language}."}
            messages = [system_msg]
            if self._is_search_request(text):
                try:
                    search_results = SearchService.search(text)
                except Exception as e:
                    search_results = None
                    print("search error:", e)
                if search_results:
                    messages.append({"role": "system", "content": (
                        "Resultats de recherche web (DuckDuckGo) :\n" + search_results +
                        "\nUtilise ces infos pour repondre.")})
                else:
                    messages.append({"role": "system", "content": "Recherche web sans resultat."})
            history = getattr(app, "current_conversation_messages", [])
            for msg in history[-20:]:
                role = "user" if msg.get("is_user") else "assistant"
                messages.append({"role": role, "content": msg.get("text", "")})
            response = app.client.chat.completions.create(model="llama-3.3-70b-versatile", messages=messages)
            reply = response.choices[0].message.content
        except Exception as e:
            reply = f"Erreur : {e}"
        Clock.schedule_once(lambda dt: self.replace_last_bubble(reply))

    def replace_last_bubble(self, reply):
        chat_box = self.ids.chat_box
        if chat_box.children:
            chat_box.remove_widget(chat_box.children[0])
        self.add_message(reply, is_user=False)
        App.get_running_app().add_message_to_history(reply, is_user=False)
        self.set_status("systeme en ligne", busy=False)
        if "orb" in self.ids:
            self.ids.orb.pulse_speaking(len(reply))
        # Lecture vocale de la réponse en arrière-plan
        threading.Thread(target=VoiceService.speak_text, args=(reply,), daemon=True).start()


class JarvisApp(App):
    title = "Jarvis IA"
    client = None
    root_widget = None
    sm = None
    bg_service = None
    primary_color = ListProperty(list(THEMES["Cyan"]))
    language = StringProperty("Français")
    theme_name = StringProperty("Cyan")
    default_city = StringProperty("")

    def build(self):
        Builder.load_string(KV)
        self.sm = ScreenManager(transition=FadeTransition(duration=0.4))
        boot = BootScreen(name="boot")
        chat_screen = ChatScreen(name="chat")
        self.root_widget = RootWidget()
        chat_screen.add_widget(self.root_widget)
        self.sm.add_widget(boot)
        self.sm.add_widget(chat_screen)
        self.sm.current = "boot"
        
        self.bg_service = JarvisBackgroundService()
        self.bg_service.start_listening()
        
        return self.sm

    def on_pause(self):
        return True 

    def on_resume(self):
        if self.bg_service and not self.bg_service.is_listening:
            self.bg_service.start_listening()

    def check_api_key(self):
        self.load_settings()
        self.start_new_conversation()
        try:
            LocationService.request_permission()
        except Exception as e:
            print("location permission request error:", e)
        key = self.get_saved_key()
        if key:
            self.init_client(key)
            self.root_widget.add_message(
                "Bonjour. Tous les systemes sont operationnels. Comment puis-je vous etre utile ?", is_user=False)
        else:
            self.ask_api_key()

    def _read_config(self):
        config_path = os.path.join(self.user_data_dir, CONFIG_FILE)
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _write_config(self, updates):
        data = self._read_config()
        data.update(updates)
        config_path = os.path.join(self.user_data_dir, CONFIG_FILE)
        try:
            with open(config_path, "w") as f:
                json.dump(data, f)
        except Exception as e:
            print("write_config error:", e)

    def load_appointments(self):
        config = self._read_config()
        return config.get("appointments", [])

    def save_appointment(self, title, target_datetime):
        config = self._read_config()
        appointments = config.get("appointments", [])
        import time
        appt = {
            "id": str(int(time.time() * 1000)),
            "title": title,
            "datetime": target_datetime.isoformat(),
            "created": datetime.now().isoformat(),
        }
        appointments.append(appt)
        appointments.sort(key=lambda x: x["datetime"])
        config["appointments"] = appointments
        self._write_config(config)

    def delete_appointment(self, appt_id):
        config = self._read_config()
        appointments = config.get("appointments", [])
        appointments = [a for a in appointments if a.get("id") != appt_id]
        config["appointments"] = appointments
        self._write_config(config)

    def load_shopping_list(self):
        config = self._read_config()
        return config.get("shopping_list", [])

    def add_to_shopping_list(self, item):
        config = self._read_config()
        shopping_list = config.get("shopping_list", [])
        
        item_lower = item.lower()
        if not any(existing.lower() == item_lower for existing in shopping_list):
            shopping_list.append(item)
            config["shopping_list"] = shopping_list
            self._write_config(config)

    def remove_from_shopping_list(self, item):
        config = self._read_config()
        shopping_list = config.get("shopping_list", [])
        shopping_list = [i for i in shopping_list if i != item]
        config["shopping_list"] = shopping_list
        self._write_config(config)

    def load_conversations(self):
        conv_path = os.path.join(self.user_data_dir, CONVERSATIONS_FILE)
        if os.path.exists(conv_path):
            try:
                with open(conv_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print("load_conversations error:", e)
        return []

    def save_conversations(self, conversations):
        try:
            conv_path = os.path.join(self.user_data_dir, CONVERSATIONS_FILE)
            with open(conv_path, "w", encoding="utf-8") as f:
                json.dump(conversations, f, ensure_ascii=False)
        except Exception as e:
            print("save_conversations error:", e)

    def start_new_conversation(self):
        import time
        self.current_conversation_id = str(int(time.time() * 1000))
        self.current_conversation_title = None
        self.current_conversation_messages = []

    def add_message_to_history(self, text, is_user):
        if not hasattr(self, "current_conversation_messages"):
            self.start_new_conversation()
        self.current_conversation_messages.append({"text": text, "is_user": is_user})
        if self.current_conversation_title is None and is_user:
            title = text.strip()
            if len(title) > 45:
                title = title[:45] + "..."
            self.current_conversation_title = title or "Nouvelle conversation"
        conversations = self.load_conversations()
        conversations = [c for c in conversations if c.get("id") != self.current_conversation_id]
        conversations.insert(0, {
            "id": self.current_conversation_id,
            "title": self.current_conversation_title or "Nouvelle conversation",
            "messages": self.current_conversation_messages,
        })
        self.save_conversations(conversations[:50])

    def load_settings(self):
        data = self._read_config()
        theme_name = data.get("theme", "Cyan")
        if theme_name in THEMES:
            self.theme_name = theme_name
            self.primary_color = list(THEMES[theme_name])
        self.language = data.get("language", "Français")
        self.default_city = data.get("default_city", "")

    def set_theme(self, theme_name):
        if theme_name in THEMES:
            self.theme_name = theme_name
            self.primary_color = list(THEMES[theme_name])
            self._write_config({"theme": theme_name})

    def set_language(self, language):
        self.language = language
        self._write_config({"language": language})

    def set_default_city(self, city):
        self.default_city = city
        self._write_config({"default_city": city})

    def get_saved_key(self):
        return self._read_config().get("groq_api_key")

    def save_key(self, key):
        self._write_config({"groq_api_key": key})

    def ask_api_key(self):
        layout = BoxLayout(orientation="vertical", spacing=dp(12), padding=dp(20))
        layout.add_widget(Label(text="[b][color=00d9ff]INITIALISATION REQUISE[/color][/b]\nEntrez votre cle API Groq",
                                 markup=True, halign="center"))
        key_input = TextInput(multiline=False, size_hint_y=None, height=dp(42))
        layout.add_widget(key_input)
        btn = Button(text="ACTIVER JARVIS", size_hint_y=None, height=dp(48), bold=True,
                      background_normal="", background_color=(0, 0.85, 1, 1), color=(0.02, 0.05, 0.06, 1))
        layout.add_widget(btn)
        popup = Popup(title="", content=layout, size_hint=(0.88, 0.42), auto_dismiss=False,
                       background_color=(0.02, 0.05, 0.07, 0.97))
        def validate(instance):
            key = key_input.text.strip()
            if key:
                self.save_key(key)
                self.init_client(key)
                popup.dismiss()
                self.root_widget.add_message(
                    "Bonjour. Tous les systemes sont operationnels. Comment puis-je vous etre utile ?", is_user=False)
        btn.bind(on_release=validate)
        popup.open()

    def init_client(self, key):
        try:
            from groq import Groq
            self.client = Groq(api_key=key)
        except Exception as e:
            self.client = None
            if self.root_widget:
                self.root_widget.add_message(f"ERREUR D'INITIALISATION : {e}", is_user=False)


from kivy.base import ExceptionHandler, ExceptionManager
import traceback

class CrashCatcher(ExceptionHandler):
    def handle_exception(self, inst):
        try:
            err_text = "".join(traceback.format_exception(type(inst), inst, inst.__traceback__))
            err_text = err_text.encode('ascii', 'ignore').decode('ascii')
        except Exception:
            err_text = str(inst)
        print("CRASH CAPTURE:", err_text)
        app = App.get_running_app()
        try:
            if app and app.root_widget:
                if app.sm and app.sm.current != "chat":
                    app.sm.current = "chat"
                safe_msg = f"CRASH DETECTE: {err_text[-500:]}"
                app.root_widget.add_message(safe_msg, is_user=False)
        except Exception as e:
            print("Could not display crash:", e)
        return ExceptionManager.PASS

ExceptionManager.add_handler(CrashCatcher())

if __name__ == "__main__":
    JarvisApp().run()