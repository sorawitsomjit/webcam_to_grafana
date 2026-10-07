"""Read the pump LCD by asking a vision-language model, over an OpenAI-compatible API.

Replaces the YOLO + EasyOCR path for this branch. The model is hosted on the
organisation's own infrastructure (see config.VLM_BASE_URL); it is sent the ROI
crop and asked to transcribe the display. Turning that text into a pressure,
and deciding whether to trust it, stays with the existing pressure_ocr.parse_pressure
/ check_mode / validate_reading and the window voting in app.py.

The API key is never kept in config.py (which is committed). It is read from the
NARIT_LLM_API_KEY environment variable, or from a `.env` file next to this module
(gitignored): a line like  NARIT_LLM_API_KEY=sk-...
"""
import base64
import os

import cv2

import config

ENV_KEY = "NARIT_LLM_API_KEY"

PROMPT = (
    "This is a photo of a 2-line dot-matrix LCD on a vacuum pump controller. "
    "Transcribe every character on the display exactly as shown, line by line. "
    "Zeros are drawn with a slash through them - write them as the digit 0. "
    "A pressure reading looks like 6.3E-6 (one digit, dot, one digit, 'E', exponent). "
    "Reply with only the text of the display, one line per row, and nothing else. "
    "If you cannot read a character, write ? for it instead of guessing."
)


def _load_dotenv():
    path = os.path.join(config.BASE_DIR, ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def encode_jpeg_b64(crop, upscale=1.0):
    if upscale and upscale != 1.0:
        crop = cv2.resize(crop, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
    ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise RuntimeError("could not JPEG-encode the crop")
    return base64.b64encode(buf.tobytes()).decode("ascii")


class VLMReader:
    def __init__(self):
        self._client = None
        self._model = None

    def _ensure_client(self):
        if self._client is not None:
            return
        _load_dotenv()
        key = os.environ.get(ENV_KEY)
        if not key:
            raise RuntimeError(f"{ENV_KEY} is not set (put it in .env next to app.py)")
        from openai import OpenAI
        self._client = OpenAI(base_url=config.VLM_BASE_URL, api_key=key,
                              timeout=config.VLM_TIMEOUT, max_retries=0)
        self._model = config.VLM_MODEL or self._pick_model()

    def _pick_model(self):
        ids = [m.id for m in self._client.models.list().data]
        if not ids:
            raise RuntimeError("the server lists no models; set VLM_MODEL in config.py")
        # Prefer something that looks like the DeepSeek flash model, else the first.
        for i in ids:
            if "deepseek" in i.lower() and "flash" in i.lower():
                return i
        return ids[0]

    def list_models(self):
        self._ensure_client()
        return [m.id for m in self._client.models.list().data]

    @property
    def model(self):
        return self._model

    def read_text(self, crop, upscale=1.0):
        """-> the model's transcription of the LCD, as a string. Raises on API errors."""
        self._ensure_client()
        b64 = encode_jpeg_b64(crop, upscale)
        resp = self._client.chat.completions.create(
            model=self._model,
            temperature=0,
            max_tokens=config.VLM_MAX_TOKENS,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]}],
        )
        choice = resp.choices[0]
        text = (choice.message.content or "").strip()
        if not text and choice.finish_reason == "length":
            # A reasoning model spends tokens thinking before it answers; if the
            # budget runs out first, the reply comes back empty.
            raise RuntimeError(f"reply cut off before any answer (raise VLM_MAX_TOKENS, now {config.VLM_MAX_TOKENS})")
        return text
