"""
Модуль локального (офлайн) распознавания голосовых сообщений без траты токенов Gemini.
Использует бесплатную офлайн-модель Vosk (vosk-model-small-ru-0.22, 45 МБ)
и ffmpeg / pydub / soundfile для конвертации голосовых сообщений ВК (.ogg / .mp3) в PCM WAV.
"""

import os
import io
import json
import wave
import zipfile
import logging
import tempfile
import subprocess
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
MODELS_DIR = BASE_DIR / "models"
VOSK_MODEL_NAME = "vosk-model-small-ru-0.22"
VOSK_MODEL_PATH = MODELS_DIR / VOSK_MODEL_NAME
VOSK_MODEL_URL = f"https://alphacephei.com/vosk/models/{VOSK_MODEL_NAME}.zip"

_vosk_model = None
_vosk_init_attempted = False


def ensure_vosk_model():
    """
    Проверяет наличие локальной офлайн-модели Vosk для русского языка.
    Если пакет vosk установлен, но папка модели еще не скачана — автоматически
    загружает компактную русскую модель (45 МБ) при первом запуске.
    """
    global _vosk_model, _vosk_init_attempted
    if _vosk_model is not None:
        return _vosk_model
    if _vosk_init_attempted:
        return None

    _vosk_init_attempted = True
    try:
        from vosk import Model, SetLogLevel
        SetLogLevel(-1)
    except ImportError:
        logger.info("Библиотека 'vosk' не установлена (pip install vosk). Используется расшифровка ВК / Gemini.")
        return None

    try:
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        if not VOSK_MODEL_PATH.exists():
            logger.info(f"📥 Скачивание бесплатной офлайн-модели речи {VOSK_MODEL_NAME} (~45 МБ)...")
            zip_path = MODELS_DIR / f"{VOSK_MODEL_NAME}.zip"
            urllib.request.urlretrieve(VOSK_MODEL_URL, str(zip_path))
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(MODELS_DIR)
            try:
                zip_path.unlink()
            except OSError:
                pass
            logger.info(f"✅ Локальная модель речи {VOSK_MODEL_NAME} успешно установлена!")

        _vosk_model = Model(str(VOSK_MODEL_PATH))
        return _vosk_model
    except Exception as e:
        logger.warning(f"Не удалось инициализировать локальную модель Vosk: {e}")
        return None


def convert_audio_to_pcm_wav(audio_bytes: bytes) -> Optional[bytes]:
    """
    Конвертирует аудио (.ogg / .mp3) в моно WAV 16000 Гц 16-bit PCM через ffmpeg или pydub.
    """
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as in_file:
        in_file.write(audio_bytes)
        in_path = in_file.name

    out_path = in_path + ".wav"
    try:
        # 1. Пробуем системный ffmpeg (самый быстрый и надежный способ)
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", in_path, "-ar", "16000", "-ac", "1", "-f", "wav", out_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15
        )
        if proc.returncode == 0 and os.path.exists(out_path):
            with open(out_path, "rb") as f:
                return f.read()
    except Exception:
        pass
    finally:
        try:
            if os.path.exists(in_path):
                os.remove(in_path)
            if os.path.exists(out_path):
                os.remove(out_path)
        except OSError:
            pass

    # 2. Пробуем soundfile / librosa при отсутствии ffmpeg в PATH
    try:
        import soundfile as sf
        data, samplerate = sf.read(io.BytesIO(audio_bytes))
        wav_io = io.BytesIO()
        sf.write(wav_io, data, samplerate, format="WAV", subtype="PCM_16")
        return wav_io.getvalue()
    except Exception:
        pass

    return None


def transcribe_audio_locally(audio_bytes: bytes, user_schedule: list = None) -> Optional[str]:
    """
    Распознает голосовое сообщение полностью локально на процессоре (0 токенов Gemini):
    1. Через офлайн-движок Vosk (vosk-model-small-ru-0.22).
    2. Если Vosk недоступен — пробует SpeechRecognition.
    3. После распознавания применяет нечёткую коррекцию (local_nlp) с учётом расписания студента.
    Возвращает распознанный текст или None.
    """
    if not audio_bytes:
        return None

    model = ensure_vosk_model()
    if model is not None:
        wav_bytes = convert_audio_to_pcm_wav(audio_bytes)
        if wav_bytes:
            try:
                from vosk import KaldiRecognizer
                with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                    rec = KaldiRecognizer(model, wf.getframerate())
                    rec.SetWords(False)
                    parts = []
                    while True:
                        data = wf.readframes(4000)
                        if len(data) == 0:
                            break
                        if rec.AcceptWaveform(data):
                            res = json.loads(rec.Result())
                            if res.get("text"):
                                parts.append(res["text"])
                    final_res = json.loads(rec.FinalResult())
                    if final_res.get("text"):
                        parts.append(final_res["text"])

                    full_text = " ".join(parts).strip()
                    if full_text:
                        logger.info(f"🎙️ [Vosk Offline] Голосовое распознано локально (0 токенов): «{full_text}»")
                        
                        # Применяем нечёткую коррекцию через local_nlp (исправляем ошибки Vosk)
                        if user_schedule:
                            try:
                                from local_nlp import correct_transcript
                                corrected = correct_transcript(full_text, user_schedule)
                                if corrected and corrected != full_text:
                                    logger.info(f"🔧 [NLP] Текст после коррекции: «{corrected}»")
                                    full_text = corrected
                            except ImportError:
                                pass
                            except Exception as nlp_err:
                                logger.debug(f"NLP-коррекция пропущена: {nlp_err}")
                        
                        return full_text
            except Exception as e:
                logger.warning(f"Ошибка при локальном распознавании Vosk: {e}")

    return None
