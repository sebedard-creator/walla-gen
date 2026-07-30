import os
import tempfile
import requests
import threading
import time
import re
import json
import uuid
import unicodedata
import mimetypes
import shutil
import logging
import sys
import secrets
import struct
import subprocess
from datetime import datetime
from collections.abc import Iterable
from html import escape
import xml.etree.ElementTree as ET
from flask import Flask, render_template, request, jsonify, send_file
import anthropic
import replicate
from dotenv import load_dotenv

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    force=True
)

load_dotenv()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max upload
replicate_lock = threading.Lock()
last_replicate_call = 0.0
voice_library_lock = threading.Lock()
VOICE_LIBRARY_DIR = os.path.join(app.root_path, 'voice_library')
VOICE_LIBRARY_INDEX = os.path.join(VOICE_LIBRARY_DIR, 'index.json')
VOICE_LIBRARY_EXTENSIONS = {'.wav', '.mp3', '.ogg', '.flac', '.m4a'}
APP_CACHE_DIR = os.path.join(app.root_path, 'cache')
CACHE_INPUT_DIR = os.path.join(APP_CACHE_DIR, 'input')
CACHE_OUTPUT_DIR = os.path.join(APP_CACHE_DIR, 'output')
LOCAL_CA_CERT = os.path.join(app.root_path, 'certs', 'walla-local-ca.crt')
CACHE_DEBUG_FILES = (
    'last-generation-debug.txt',
    'last-transcription-debug.txt'
)
WALLA_SLOT_MAX_SECONDS = float(os.getenv('WALLA_SLOT_MAX_SECONDS', '120'))
DEFAULT_WALLA_TEMPLATE_PATH = os.getenv('WALLA_TEMPLATE_PATH', 'walla_template.ptx')


def get_pt_api_module():
    """Load the local pt_api checkout without making its path machine-specific."""
    configured_path = (os.getenv('PT_API_PATH') or '').strip()
    candidates = []
    if configured_path:
        candidates.append(resolve_app_path(configured_path))
    candidates.append(os.path.abspath(os.path.join(app.root_path, '..', 'pt_api')))

    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, 'pt_api.py')) and candidate not in sys.path:
            sys.path.insert(0, candidate)

    try:
        import pt_api
    except ImportError as exc:
        raise RuntimeError(
            "pt_api est introuvable. Installez-le dans l'environnement Python ou configurez PT_API_PATH."
        ) from exc

    if not hasattr(pt_api.ProToolsSession, 'get_timeline_clip_groups'):
        raise RuntimeError(
            "pt_api 1.4.0 ou une version plus récente est requise pour lire les Clip Groups."
        )
    return pt_api


def ensure_voice_library():
    os.makedirs(VOICE_LIBRARY_DIR, exist_ok=True)


def ensure_app_cache():
    os.makedirs(CACHE_INPUT_DIR, exist_ok=True)
    os.makedirs(CACHE_OUTPUT_DIR, exist_ok=True)


def resolve_app_path(path):
    if not path:
        return ''
    path = os.path.expanduser(path)
    return path if os.path.isabs(path) else os.path.join(app.root_path, path)


def safe_cache_path(path):
    base = os.path.abspath(APP_CACHE_DIR)
    target = os.path.abspath(path)
    return os.path.commonpath([base, target]) == base


def named_cache_file(kind, suffix):
    ensure_app_cache()
    if kind == 'input':
        directory = CACHE_INPUT_DIR
        prefix = 'input_'
    elif kind == 'output':
        directory = CACHE_OUTPUT_DIR
        prefix = 'output_'
    else:
        raise ValueError("Type de cache invalide.")
    return tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix=prefix, dir=directory)


def audio_upload_suffix(filename, mimetype):
    suffix = os.path.splitext(filename or '')[1].lower()
    if suffix in ('.webm', '.ogg', '.wav', '.mp3', '.m4a', '.flac'):
        return suffix

    mimetype = (mimetype or '').lower()
    if 'ogg' in mimetype:
        return '.ogg'
    if 'webm' in mimetype:
        return '.webm'
    if 'wav' in mimetype:
        return '.wav'
    if 'mpeg' in mimetype or 'mp3' in mimetype:
        return '.mp3'
    if 'mp4' in mimetype or 'm4a' in mimetype:
        return '.m4a'
    return '.webm'


def clear_directory_contents(directory):
    ensure_app_cache()
    if not safe_cache_path(directory):
        raise ValueError("Refus de nettoyer un dossier hors du cache de l'application.")

    deleted_files = 0
    deleted_bytes = 0
    errors = []

    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path) or os.path.islink(path):
                deleted_bytes += os.path.getsize(path)
                os.unlink(path)
                deleted_files += 1
            elif os.path.isdir(path):
                for root, _, files in os.walk(path):
                    for file_name in files:
                        file_path = os.path.join(root, file_name)
                        deleted_bytes += os.path.getsize(file_path)
                        deleted_files += 1
                shutil.rmtree(path)
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    return deleted_files, deleted_bytes, errors


def clear_app_cache():
    input_count, input_bytes, input_errors = clear_directory_contents(CACHE_INPUT_DIR)
    output_count, output_bytes, output_errors = clear_directory_contents(CACHE_OUTPUT_DIR)

    debug_count = 0
    debug_bytes = 0
    debug_errors = []
    for filename in CACHE_DEBUG_FILES:
        path = os.path.join(app.root_path, filename)
        try:
            if os.path.exists(path):
                debug_bytes += os.path.getsize(path)
                os.unlink(path)
                debug_count += 1
        except Exception as exc:
            debug_errors.append(f"{filename}: {exc}")

    return {
        'deleted_files': input_count + output_count + debug_count,
        'freed_bytes': input_bytes + output_bytes + debug_bytes,
        'errors': input_errors + output_errors + debug_errors
    }


def load_voice_library():
    ensure_voice_library()
    if not os.path.exists(VOICE_LIBRARY_INDEX):
        return []
    with open(VOICE_LIBRARY_INDEX, 'r', encoding='utf-8') as index_file:
        try:
            data = json.load(index_file)
        except json.JSONDecodeError:
            return []
    return data if isinstance(data, list) else []


def save_voice_library(items):
    ensure_voice_library()
    tmp_path = os.path.join(VOICE_LIBRARY_DIR, 'index.tmp')
    with open(tmp_path, 'w', encoding='utf-8') as index_file:
        json.dump(items, index_file, ensure_ascii=False, indent=2)
    os.replace(tmp_path, VOICE_LIBRARY_INDEX)


def safe_voice_filename_part(name):
    text = unicodedata.normalize('NFD', name)
    text = re.sub(r'[\u0300-\u036f]', '', text)
    text = re.sub(r'[^A-Za-z0-9_-]+', '_', text).strip('_')
    return (text[:40] or 'voice').lower()


def public_voice_item(item):
    return {
        'id': item.get('id', ''),
        'name': item.get('name', ''),
        'transcript': item.get('transcript', ''),
        'created_at': item.get('created_at', '')
    }


def find_voice_library_item(voice_id):
    for item in load_voice_library():
        if item.get('id') == voice_id:
            return item
    return None


def voice_audio_path(item):
    base = os.path.abspath(VOICE_LIBRARY_DIR)
    path = os.path.abspath(os.path.join(base, item.get('filename', '')))
    if not path.startswith(base + os.sep):
        raise ValueError("Chemin de voix sauvegardée invalide.")
    if not os.path.exists(path):
        raise FileNotFoundError("Le fichier audio de cette voix sauvegardée est introuvable.")
    return path


def prune_missing_voice_library_items():
    with voice_library_lock:
        items = load_voice_library()
        kept = []

        for item in items:
            try:
                voice_audio_path(item)
            except (FileNotFoundError, ValueError):
                continue
            kept.append(item)

        if len(kept) != len(items):
            save_voice_library(kept)

    return kept


def normalized_label_token(value):
    value = unicodedata.normalize('NFD', str(value or ''))
    value = ''.join(char for char in value if not unicodedata.combining(char))
    return re.sub(r'[^A-Z0-9]+', '', value.upper())


def parse_walla_language(value):
    token = normalized_label_token(value)
    if token in ('FR', 'FRA', 'FRAN', 'FRENCH', 'FRANCAIS', 'FRANCAISE'):
        return 'fr'
    if token in ('EN', 'ENG', 'ENGLISH', 'ANGLAIS', 'ANGLAISE'):
        return 'en'
    return None


def parse_walla_gender(value):
    token = normalized_label_token(value)
    if token in ('F', 'FEMALE', 'FEMME', 'FEMININ', 'WOMAN'):
        return 'female'
    if token in ('M', 'MALE', 'HOMME', 'MASCULIN', 'MAN'):
        return 'male'
    return None


def parse_walla_slot_name(group_name):
    """Parse compact Clip Group labels: ``F F scénario`` / ``A H scenario``."""
    parts = str(group_name or '').strip().split(maxsplit=2)
    if len(parts) != 3:
        raise ValueError(
            "Le nom doit suivre le format « F F scénario », « F H scénario », « A F scenario » ou « A H scenario »."
        )

    language = {'F': 'fr', 'A': 'en'}.get(normalized_label_token(parts[0]))
    gender = {'F': 'female', 'H': 'male'}.get(normalized_label_token(parts[1]))
    scenario = parts[2].strip().strip('"\'«»“”').strip()
    if not language:
        raise ValueError("Le premier code doit être F (français) ou A (anglais).")
    if not gender:
        raise ValueError("Le deuxième code doit être F (female) ou H (homme/male).")
    if not scenario:
        raise ValueError("Le scénario après les deux codes est requis.")
    return {'language': language, 'gender': gender, 'scenario': scenario}


def voice_walla_metadata(voice):
    """Read optional future metadata, then fall back to the established name prefix."""
    language = normalize_language(voice.get('language')) if voice.get('language') else None
    gender = parse_walla_gender(voice.get('gender')) if voice.get('gender') else None
    name = (voice.get('name') or '').strip()

    parts = re.split(r'\s*(?:\||[-–—]|:)\s*', name, maxsplit=2)
    if len(parts) >= 2:
        language = language or parse_walla_language(parts[0])
        gender = gender or parse_walla_gender(parts[1])

    # Accept concise names such as "FRAN Female - Marie" too.
    if not language or not gender:
        match = re.match(
            r'^\s*(FRAN(?:CAIS(?:E)?)?|FR|ENG(?:LISH)?|EN|ANGLAIS(?:E)?)\b[\s_\-:|]*'
            r'(FEMALE|FEMME|F|MALE|HOMME|M)\b',
            name,
            flags=re.IGNORECASE,
        )
        if match:
            language = language or parse_walla_language(match.group(1))
            gender = gender or parse_walla_gender(match.group(2))

    return {'language': language, 'gender': gender}


def walla_voice_candidates(language, gender):
    candidates = []
    for voice in prune_missing_voice_library_items():
        metadata = voice_walla_metadata(voice)
        language_matches = metadata['language'] in (None, language)
        gender_matches = metadata['gender'] == gender
        if language_matches and gender_matches:
            candidates.append(voice)
    return candidates


def make_walla_slot_preview(group):
    parsed = parse_walla_slot_name(group['group_name'])
    length_samples = int(group['length_samples'])
    if length_samples <= 0:
        raise ValueError("La durée du Clip Group doit être supérieure à zéro.")
    duration_seconds = length_samples / 48_000
    if duration_seconds > WALLA_SLOT_MAX_SECONDS:
        raise ValueError(
            f"Le Clip Group dure {duration_seconds:.1f} s; la limite configurée est {WALLA_SLOT_MAX_SECONDS:.0f} s."
        )
    candidates = walla_voice_candidates(parsed['language'], parsed['gender'])
    if not candidates:
        language_label = 'FRAN' if parsed['language'] == 'fr' else 'ENG'
        raise ValueError(
            f"Aucune voix de librairie compatible ({language_label} / {parsed['gender']}). "
            "Nommez les voix, par exemple, « FRAN - Female - Marie ».")
    return {
        **group,
        **parsed,
        'duration_seconds': round(duration_seconds, 3),
        'candidate_voice_count': len(candidates),
        'candidate_voice_ids': [voice['id'] for voice in candidates],
    }


def inspect_walla_slots(session_path, template_path=None):
    pt_api = get_pt_api_module()
    session = pt_api.ProToolsSession(session_path)
    if session.sample_rate != 48_000:
        raise ValueError("La session qui contient les Clip Groups doit être à 48 kHz.")

    template_tracks = []
    if template_path:
        template = pt_api.ProToolsSession(template_path)
        if template.sample_rate != 48_000:
            raise ValueError("La template PTX doit être à 48 kHz.")
        template_tracks = template.get_tracks()
        try:
            # pt_api 1.4.0 has no public template-preflight method yet. Reuse
            # its read-only validator here so this fails before any billable
            # script or TTS request is made.
            template._validated_audio_import_template()
        except ValueError as exc:
            raise ValueError(
                "La template PTX n'est pas prête pour l'import audio automatique : "
                f"{exc} Créez-la selon les instructions affichées dans l'interface."
            ) from exc

    slots = []
    errors = []
    for group in session.get_timeline_clip_groups():
        try:
            slot = make_walla_slot_preview(group)
            if template_tracks and slot['track'] not in template_tracks:
                raise ValueError(
                    f"La piste « {slot['track']} » n'existe pas dans la template PTX."
                )
            slots.append(slot)
        except ValueError as exc:
            errors.append({
                'group_id': group.get('group_id'),
                'group_name': group.get('group_name', ''),
                'track': group.get('track', ''),
                'error': str(exc),
            })

    return {
        'slots': slots,
        'errors': errors,
        'template_tracks': template_tracks,
        'sample_rate': session.sample_rate,
    }


def wait_for_replicate_slot():
    global last_replicate_call
    spacing = float(os.getenv('REPLICATE_MIN_SECONDS_BETWEEN_CALLS', '12'))
    with replicate_lock:
        elapsed = time.monotonic() - last_replicate_call
        if elapsed < spacing:
            time.sleep(spacing - elapsed)
        last_replicate_call = time.monotonic()


def text_from_anthropic_message(message):
    for block in message.content:
        text = getattr(block, 'text', None)
        if text:
            return text.strip()
    raise ValueError("La réponse Anthropic ne contient pas de texte.")


def normalize_language(value):
    return 'en' if str(value or '').strip().lower() in ('en', 'eng', 'english', 'anglais') else 'fr'


def language_name(language):
    return 'English' if normalize_language(language) == 'en' else 'French'


def prepare_quebec_tts_script(script, language='fr'):
    """Adapt standard text into a Quebec TTS guide before sending it to Replicate."""
    if normalize_language(language) == 'en':
        return script

    enabled = os.getenv('QUEBEC_TTS_REWRITE', 'true').lower() in ('1', 'true', 'yes', 'on')
    if not enabled:
        return script

    client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
    prompt = f"""Tu es directeur de plateau voix au Québec et spécialiste TTS.

Le modèle audio qui lira ce texte a tendance à produire un accent français de France.
Ta tâche est de réécrire le texte pour forcer une diction, une prosodie et un débit
nettement québécois, tout en conservant le sens, la durée approximative et un ton crédible.

Règles :
- Le résultat doit sonner québécois à voix haute, pas français de France.
- Utilise des tournures québécoises naturelles et des indices d'oralité utiles au TTS.
- Tu peux employer quelques graphies orales québécoises si elles aident la prononciation.
- Évite la caricature, mais sois plus affirmé que du français standard.
- Évite absolument les expressions, le vocabulaire et la cadence typiques de France.
- N'utilise jamais le mot « Yo », ni comme salutation ni ailleurs dans le texte.
- Ne donne aucune explication, aucun titre, aucune note.
- Retourne uniquement le texte qui doit être lu par le TTS.

Texte à adapter :
{script}"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}]
    )
    return text_from_anthropic_message(message)


def normalize_tts_text(text):
    return ' '.join(text.split())


def strip_inline_stage_directions(text):
    text = re.sub(r'\*[^*]{1,120}\*', '', text)
    text = re.sub(r'\[[^\]]{1,120}\]', '', text)
    text = re.sub(r'\([^)]{1,120}\)', '', text)
    return normalize_tts_text(text)


def log_generation_payload(ref_text, tts_script, voice_direction):
    with open('last-generation-debug.txt', 'w', encoding='utf-8') as log:
        log.write(f"timestamp={datetime.now().isoformat(timespec='seconds')}\n\n")
        log.write("ref_text:\n")
        log.write(ref_text)
        log.write("\n\n")
        log.write("tts_script:\n")
        log.write(tts_script)
        log.write("\n\n")
        log.write("voice_direction:\n")
        log.write(voice_direction)
        log.write("\n")


def build_tts_input(model, tts_script, ref_audio, model_ref_text, voice_direction, language='fr'):
    language = normalize_language(language)

    if language == 'en':
        style_instruction = (
            "Speak in natural English with clear diction, matching the reference voice. "
            "Do not add a French accent unless it exists in the reference."
        )
    else:
        style_instruction = (
            "Parle en français québécois naturel, avec une diction claire, "
            "un accent québécois crédible et aucune cadence française de France."
        )
    if voice_direction:
        label = "Performance direction" if language == 'en' else "Direction de jeu"
        style_instruction += f" {label}: {voice_direction[:180]}"

    return {
        "text": tts_script,
        "mode": "voice_clone",
        "language": language_name(language),
        "reference_audio": ref_audio,
        "reference_text": model_ref_text,
        "style_instruction": style_instruction
    }


def log_transcription(transcript):
    with open('last-transcription-debug.txt', 'w', encoding='utf-8') as log:
        log.write(f"timestamp={datetime.now().isoformat(timespec='seconds')}\n\n")
        log.write(transcript)
        log.write("\n")


def transcript_part_to_text(part):
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        for key in ('text', 'transcription', 'output'):
            value = part.get(key)
            if value:
                return transcript_part_to_text(value)
    return str(part)


def fix_mojibake(text):
    markers = ('Ã', 'Â', 'Å')
    if not any(marker in text for marker in markers):
        return text
    try:
        fixed = text.encode('latin1').decode('utf-8')
    except UnicodeError:
        return text

    old_score = sum(text.count(marker) for marker in markers)
    new_score = sum(fixed.count(marker) for marker in markers)
    return fixed if new_score < old_score else text


def extract_transcript(output):
    if isinstance(output, str):
        return fix_mojibake(output).strip()
    if isinstance(output, dict):
        for key in ('transcription', 'text', 'output'):
            value = output.get(key)
            if value:
                return extract_transcript(value)
    if isinstance(output, Iterable):
        text = ''.join(transcript_part_to_text(item) for item in output if item)
        if text.strip():
            return fix_mojibake(text).strip()
    return fix_mojibake(str(output)).strip()


def run_transcription(replicate_client, model, ref_audio, language='fr'):
    language = normalize_language(language)
    prompt = (
        "Exact transcription in English."
        if language == 'en'
        else "Transcription exacte en français québécois."
    )
    wait_for_replicate_slot()
    return replicate_client.run(
        model,
        input={
            "audio_file": ref_audio,
            "language": language,
            "temperature": 0,
            "prompt": prompt
        }
    )


def generate_walla_script(scenario, duration_seconds, language):
    language = normalize_language(language)
    client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
    duration_seconds = max(1, round(float(duration_seconds), 1))
    if language == 'en':
        prompt = f"""Write the spoken lines of one person in a natural background conversation.

Scenario: {scenario}
Target duration: approximately {duration_seconds} seconds.

Rules:
- Write only natural spoken English for a single voice.
- It must sound like believable walla/background dialogue, not a narrator or announcement.
- Use short, speakable sentences and natural punctuation.
- No speaker labels, stage directions, quotation marks, title, notes, or explanation.
- Return only the words to be spoken."""
    else:
        prompt = f"""Écris les répliques d'une seule personne dans une conversation d'ambiance naturelle.

Scénario : {scenario}
Durée visée : environ {duration_seconds} secondes.

Règles :
- Écris uniquement un français québécois parlé, naturel et crédible, pour une seule voix.
- Le résultat doit sonner comme du walla / une conversation d'arrière-plan, jamais comme une narration ou une annonce.
- Utilise des phrases courtes, faciles à prononcer et une ponctuation naturelle.
- Aucune étiquette de personnage, didascalie, guillemet, titre, note ou explication.
- Retourne uniquement les mots à prononcer."""
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    return text_from_anthropic_message(message)


def generate_walla_direction(scenario, script, language):
    language = normalize_language(language)
    client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
    if language == 'en':
        prompt = f"""Write one short performance direction for background-dialogue TTS.

Constraints: maximum 18 words; describe only tone, pace and intensity; no list; no metaphor; mention natural English.
Scenario: {scenario}
Script: {script}
Return only the direction."""
    else:
        prompt = f"""Écris une micro-direction de jeu pour un TTS de conversation d'ambiance.

Contraintes : maximum 18 mots; décris seulement le ton, le débit et l'intensité; pas de liste ni métaphore; mentionne « québécois naturel ».
Scénario : {scenario}
Texte : {script}
Retourne uniquement la direction."""
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        messages=[{"role": "user", "content": prompt}],
    )
    return text_from_anthropic_message(message)


def generate_audio_from_library_voice(script, library_voice, voice_direction, language):
    """Generate one WAV from a saved reference voice and return its cache path."""
    ref_text = (library_voice.get('transcript') or '').strip()
    if not ref_text:
        raise ValueError(f"La voix « {library_voice.get('name', '')} » n'a pas de transcription.")

    model = os.getenv(
        'REPLICATE_TTS_MODEL',
        'qwen/qwen3-tts:0b366549c7541af95a69454651f4ebf02c699036841cd20b78b9e2a26b4b2750'
    )
    ref_path = voice_audio_path(library_voice)
    tts_script = strip_inline_stage_directions(prepare_quebec_tts_script(script, language))
    model_ref_text = normalize_tts_text(ref_text)
    log_generation_payload(model_ref_text, tts_script, voice_direction)
    replicate_client = replicate.Client(api_token=os.getenv('REPLICATE_API_KEY'))
    with open(ref_path, 'rb') as ref_audio:
        model_input = build_tts_input(
            model, tts_script, ref_audio, model_ref_text, voice_direction, language
        )
        wait_for_replicate_slot()
        output = replicate_client.run(model, input=model_input)

    response = requests.get(str(output), timeout=120)
    response.raise_for_status()
    generated = named_cache_file('output', '.wav')
    generated.write(response.content)
    generated.close()
    return generated.name


def write_riff_chunk(stream, chunk_id, payload):
    stream.write(chunk_id)
    stream.write(struct.pack('<I', len(payload)))
    stream.write(payload)
    if len(payload) % 2:
        stream.write(b'\x00')


def render_pt_api_wave(source_path, destination_path, time_reference):
    """Render arbitrary TTS output as the strict BWF WAV expected by pt_api."""
    if not 0 <= int(time_reference) <= 0xFFFFFFFF:
        raise ValueError("La position audio est hors des limites BWF prises en charge.")
    raw_path = destination_path + '.f32le'
    ffmpeg = os.getenv('FFMPEG_EXECUTABLE', 'ffmpeg')
    try:
        completed = subprocess.run(
            [
                ffmpeg, '-y', '-v', 'error', '-i', source_path,
                '-map', '0:a:0', '-ac', '1', '-ar', '48000', '-f', 'f32le', raw_path,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError("ffmpeg est requis pour préparer les WAV Pro Tools.") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or 'erreur inconnue').strip()
        raise RuntimeError(f"Conversion audio ffmpeg impossible : {detail[:300]}")

    try:
        data_size = os.path.getsize(raw_path)
        if data_size == 0 or data_size % 4:
            raise ValueError("Le rendu audio converti est vide ou invalide.")
        sample_count = data_size // 4
        if sample_count > 0xFFFFFF:
            raise ValueError("Le rendu audio dépasse la durée maximale prise en charge par pt_api.")

        now = datetime.now()
        originator = b'walla-gen'.ljust(32, b'\x00')
        originator_ref = ('WALLA-' + uuid.uuid4().hex[:26]).encode('ascii').ljust(32, b'\x00')
        basic_umid = ('WALLA-GEN-' + uuid.uuid4().hex[:22]).encode('ascii').ljust(32, b'\x00')
        bext = (
            b'walla-gen generated dialogue'.ljust(256, b'\x00')
            + originator
            + originator_ref
            + now.strftime('%Y-%m-%d').encode('ascii')
            + now.strftime('%H:%M:%S').encode('ascii')
            + struct.pack('<Q', int(time_reference))
            + struct.pack('<H', 1)
            + basic_umid
            + (b'\x00' * 32)
        )
        fmt = (
            struct.pack('<HHIIHHH', 0xFFFE, 1, 48000, 192000, 4, 32, 22)
            + struct.pack('<HI', 32, 0x0004)
            + bytes.fromhex('0300000000001000800000aa00389b71')
        )
        riff_size = (
            4
            + 8 + len(bext) + (len(bext) % 2)
            + 8 + len(fmt) + (len(fmt) % 2)
            + 8 + 4
            + 8 + data_size + (data_size % 2)
        )
        with open(destination_path, 'wb') as destination, open(raw_path, 'rb') as raw:
            destination.write(b'RIFF')
            destination.write(struct.pack('<I', riff_size))
            destination.write(b'WAVE')
            write_riff_chunk(destination, b'bext', bext)
            write_riff_chunk(destination, b'fmt ', fmt)
            write_riff_chunk(destination, b'fact', struct.pack('<I', sample_count))
            destination.write(b'data')
            destination.write(struct.pack('<I', data_size))
            shutil.copyfileobj(raw, destination, length=1024 * 1024)
            if data_size % 2:
                destination.write(b'\x00')
    finally:
        if os.path.exists(raw_path):
            os.unlink(raw_path)


def safe_walla_name(value):
    value = unicodedata.normalize('NFD', str(value or ''))
    value = ''.join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r'[^A-Za-z0-9_-]+', '_', value).strip('_')
    return value[:80] or 'walla_gen'


def save_uploaded_ptx(upload, label):
    if not upload or not upload.filename:
        raise ValueError(f"Le fichier PTX {label} est requis.")
    if os.path.splitext(upload.filename)[1].lower() != '.ptx':
        raise ValueError(f"Le fichier {label} doit avoir l'extension .ptx.")
    stored = named_cache_file('input', '.ptx')
    upload.save(stored.name)
    stored.close()
    return stored.name


def resolve_walla_template(upload=None):
    """Use an explicit upload only when supplied; otherwise use the project template."""
    if upload and upload.filename:
        return save_uploaded_ptx(upload, 'template'), True
    template_path = resolve_app_path(DEFAULT_WALLA_TEMPLATE_PATH)
    if not os.path.isfile(template_path):
        raise FileNotFoundError(
            "La template walla intégrée est introuvable : " + template_path
        )
    if os.path.splitext(template_path)[1].lower() != '.ptx':
        raise ValueError("La template walla intégrée doit avoir l'extension .ptx.")
    return template_path, False


@app.route('/walla/inspect', methods=['POST'])
def inspect_walla_session():
    session_path = None
    template_path = None
    cleanup_template = False
    try:
        session_path = save_uploaded_ptx(request.files.get('session_ptx'), 'avec les Clip Groups')
        template_path, cleanup_template = resolve_walla_template(request.files.get('template_ptx'))
        return jsonify(inspect_walla_slots(session_path, template_path))
    except (ValueError, RuntimeError) as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'error': f"Lecture du PTX impossible : {str(exc)}"}), 500
    finally:
        if session_path and os.path.exists(session_path):
            os.unlink(session_path)
        if cleanup_template and template_path and os.path.exists(template_path):
            os.unlink(template_path)


@app.route('/walla/generate-session', methods=['POST'])
def generate_walla_session():
    session_path = None
    template_path = None
    cleanup_template = False
    job_directory = None
    output_directory = None
    published = False
    try:
        session_path = save_uploaded_ptx(request.files.get('session_ptx'), 'avec les Clip Groups')
        template_path, cleanup_template = resolve_walla_template(request.files.get('template_ptx'))
        inspection = inspect_walla_slots(session_path, template_path)
        if inspection['errors']:
            details = '; '.join(
                f"{item['group_name'] or 'Clip Group'} : {item['error']}"
                for item in inspection['errors']
            )
            raise ValueError(f"Les Clip Groups doivent être corrigés avant la génération : {details}")
        if not inspection['slots']:
            raise ValueError("Aucun Clip Group walla valide n'a été trouvé dans la session.")

        pt_api = get_pt_api_module()
        session_name = safe_walla_name(request.form.get('session_name') or 'walla_gen')
        job_directory = tempfile.mkdtemp(prefix='walla_job_', dir=CACHE_OUTPUT_DIR)
        render_directory = os.path.join(job_directory, 'renders')
        os.makedirs(render_directory)
        output_directory = os.path.join(
            CACHE_OUTPUT_DIR, f"{session_name}_{uuid.uuid4().hex[:10]}"
        )

        clip_specs = []
        manifest = []
        voices_by_id = {voice['id']: voice for voice in prune_missing_voice_library_items()}
        for position, slot in enumerate(inspection['slots'], start=1):
            candidates = [
                voices_by_id[voice_id]
                for voice_id in slot['candidate_voice_ids']
                if voice_id in voices_by_id
            ]
            if not candidates:
                raise ValueError(
                    f"La librairie a changé : aucune voix compatible pour « {slot['group_name']} »."
                )
            voice = secrets.choice(candidates)
            script = generate_walla_script(
                slot['scenario'], slot['duration_seconds'], slot['language']
            )
            direction = generate_walla_direction(slot['scenario'], script, slot['language'])
            generated_audio = generate_audio_from_library_voice(
                script, voice, direction, slot['language']
            )
            clip_stem = f"WALLA_{position:03d}_{safe_walla_name(slot['scenario'])}"
            compatible_audio = os.path.join(render_directory, f"{clip_stem}.wav")
            render_pt_api_wave(generated_audio, compatible_audio, slot['start_samples'])
            clip_specs.append({
                'audio_path': compatible_audio,
                'track_name': slot['track'],
                'physical_filename': f"{clip_stem}.wav",
                'clip_name': clip_stem,
                'placement_start_samples': slot['start_samples'],
            })
            manifest.append({
                'group_id': slot['group_id'],
                'group_name': slot['group_name'],
                'track': slot['track'],
                'start_samples': slot['start_samples'],
                'length_samples': slot['length_samples'],
                'duration_seconds': slot['duration_seconds'],
                'language': slot['language'],
                'gender': slot['gender'],
                'scenario': slot['scenario'],
                'voice_id': voice['id'],
                'voice_name': voice['name'],
                'script': script,
                'direction': direction,
                'audio_filename': f"{clip_stem}.wav",
            })

        build_result = pt_api.build_audio_session(
            template_path, clip_specs, output_directory, session_name=session_name
        )
        with open(os.path.join(output_directory, 'WALLA_MANIFEST.json'), 'w', encoding='utf-8') as file:
            json.dump(
                {
                    'created_at': datetime.now().isoformat(timespec='seconds'),
                    'pt_api_version': getattr(pt_api, '__version__', 'unknown'),
                    'build_result': build_result,
                    'slots': manifest,
                },
                file,
                ensure_ascii=False,
                indent=2,
            )

        archive_base = os.path.join(CACHE_OUTPUT_DIR, f"{session_name}_{uuid.uuid4().hex[:10]}")
        archive_path = shutil.make_archive(
            archive_base,
            'zip',
            root_dir=os.path.dirname(output_directory),
            base_dir=os.path.basename(output_directory),
        )
        published = True
        response = send_file(
            archive_path,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f"{session_name}.zip",
        )
        response.headers['X-Walla-Slot-Count'] = str(len(manifest))
        return response
    except (ValueError, RuntimeError) as exc:
        return jsonify({'error': str(exc)}), 400
    except replicate.exceptions.ReplicateError as exc:
        return jsonify({'error': f"Erreur Replicate : {str(exc)}"}), 500
    except Exception as exc:
        app.logger.exception('Génération walla PTX impossible')
        return jsonify({'error': f"Génération du PTX impossible : {str(exc)}"}), 500
    finally:
        if session_path and os.path.exists(session_path):
            os.unlink(session_path)
        if cleanup_template and template_path and os.path.exists(template_path):
            os.unlink(template_path)
        if job_directory and os.path.isdir(job_directory):
            shutil.rmtree(job_directory, ignore_errors=True)
        if output_directory and not published and os.path.isdir(output_directory):
            shutil.rmtree(output_directory, ignore_errors=True)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/clear-cache', methods=['POST'])
def clear_cache():
    try:
        result = clear_app_cache()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': f'Erreur lors du nettoyage du cache : {str(e)}'}), 500


@app.route('/mic-test', methods=['POST'])
def mic_test():
    audio_file = request.files.get('mic_audio')
    if not audio_file:
        return jsonify({'error': 'Aucun audio micro reçu.'}), 400

    suffix = audio_upload_suffix(audio_file.filename, audio_file.mimetype)
    tmp_audio = named_cache_file('input', suffix)

    try:
        audio_file.save(tmp_audio.name)
        tmp_audio.close()
        size = os.path.getsize(tmp_audio.name)

        replicate_client = replicate.Client(api_token=os.getenv('REPLICATE_API_KEY'))
        model = os.getenv(
            'REPLICATE_TRANSCRIBE_MODEL',
            'openai/gpt-4o-mini-transcribe:684265b6c4d23a4f5b3536a76e0b9e022ce5084f6da95fd7d0b5ebbc573a8261'
        )

        with open(tmp_audio.name, 'rb') as mic_audio:
            output = run_transcription(replicate_client, model, mic_audio, 'fr')

        transcript = extract_transcript(output)
        if not transcript:
            return jsonify({'error': 'La transcription micro est vide. Essayez de parler plus près du micro.'}), 500

        log_transcription(transcript)
        return jsonify({
            'ok': True,
            'filename': os.path.basename(tmp_audio.name),
            'bytes': size,
            'content_type': audio_file.mimetype or 'application/octet-stream',
            'transcript': transcript
        })
    except replicate.exceptions.ReplicateError as e:
        return jsonify({'error': f'Erreur Replicate transcription micro : {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': f'Erreur lors de la dictée micro : {str(e)}'}), 500


@app.route('/voice-library', methods=['GET'])
def list_voice_library():
    voices = sorted(
        (public_voice_item(item) for item in prune_missing_voice_library_items()),
        key=lambda item: item['name'].casefold()
    )
    response = jsonify({'voices': voices})
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response


@app.route('/local-ca.crt', methods=['GET'])
def download_local_ca_certificate():
    if not os.path.isfile(LOCAL_CA_CERT):
        return jsonify({'error': 'Certificat HTTPS local introuvable.'}), 404
    return send_file(
        LOCAL_CA_CERT,
        mimetype='application/x-x509-ca-cert',
        as_attachment=True,
        download_name='walla-local-ca.crt'
    )


@app.route('/voice-library/<voice_id>/audio', methods=['GET'])
def play_voice_reference(voice_id):
    item = find_voice_library_item(voice_id)
    if not item:
        return jsonify({'error': 'Voix sauvegardée introuvable.'}), 404

    try:
        path = voice_audio_path(item)
    except (FileNotFoundError, ValueError):
        prune_missing_voice_library_items()
        return jsonify({'error': 'Fichier audio de voix introuvable.'}), 404

    mimetype = mimetypes.guess_type(path)[0] or 'application/octet-stream'
    return send_file(path, mimetype=mimetype, as_attachment=False, conditional=True)


@app.route('/voice-library', methods=['POST'])
def save_voice_reference():
    name = (request.form.get('name') or '').strip()
    transcript = (request.form.get('ref_text') or '').strip()
    audio_file = request.files.get('audio_reference')

    if not name:
        return jsonify({'error': 'Le nom de la voix est requis.'}), 400
    if not audio_file:
        return jsonify({'error': 'Un fichier audio de référence est requis pour sauvegarder une voix.'}), 400
    if not transcript:
        return jsonify({'error': 'La transcription est requise pour réutiliser cette voix avec Qwen.'}), 400

    suffix = os.path.splitext(audio_file.filename)[1].lower() or '.wav'
    if suffix not in VOICE_LIBRARY_EXTENSIONS:
        return jsonify({'error': 'Format audio non supporté pour la librairie.'}), 400

    voice_id = uuid.uuid4().hex
    filename = f"{voice_id}_{safe_voice_filename_part(name)}{suffix}"
    item = {
        'id': voice_id,
        'name': name,
        'filename': filename,
        'transcript': transcript,
        'created_at': datetime.now().isoformat(timespec='seconds')
    }

    ensure_voice_library()
    audio_path = os.path.join(VOICE_LIBRARY_DIR, filename)

    with voice_library_lock:
        items = load_voice_library()
        audio_file.save(audio_path)
        items.append(item)
        save_voice_library(items)

    return jsonify({'voice': public_voice_item(item)}), 201


@app.route('/generate-script', methods=['POST'])
def generate_script():
    data = request.get_json()
    scenario = (data.get('scenario') or '').strip()
    duration = data.get('duration', 60)
    language = normalize_language(data.get('language'))

    if not scenario:
        return jsonify({'error': 'Le scénario est requis.'}), 400

    try:
        client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

        if language == 'en':
            prompt = f"""You write natural walla dialogue for film and television.

Primary objective: the text will be read by a text-to-speech / voice-cloning system.
It must therefore be natural, concise, easy to speak, and written in clear English.

Generate only the spoken lines of one person for the following situation:
"{scenario}"

The dialogue should last approximately {duration} seconds when read aloud at a natural conversational pace.
For voice cloning, keep it concise: short sentences, clear punctuation, natural rhythm, and plausible conversational replies.

Strict rules:
- Write only in English.
- Use natural spoken English, not translated-sounding French syntax.
- Return dialogue only: spoken words or replies from one person.
- Never describe the setting, actions, emotions, plot, or other characters.
- Never write narration, a scenario summary, scene directions, or explanatory prose.
- Do not add speaker names or labels; the voice should sound as if it is speaking to someone naturally.
- Do not include stage directions, titles, line numbers, notes, or parenthetical instructions.
- Return ONLY the script text, nothing else."""
        else:
            prompt = f"""Tu écris du dialogue de walla naturel pour le cinéma et la télévision québécoise.

Objectif prioritaire : le texte sera lu ensuite par un système de synthèse vocale.
Il doit donc aider à produire une diction et une prosodie clairement québécoises,
avec un accent québécois naturel et crédible, et non un accent français de France.

Génère uniquement les répliques parlées d'une seule personne pour la situation suivante :
"{scenario}"

Le dialogue doit durer approximativement {duration} secondes lorsque lu à voix haute au rythme naturel d'une conversation.
Pour le clonage vocal, reste concis : phrases courtes, ponctuation claire, débit naturel et réponses plausibles à une personne hors champ.

Règles strictes :
- Utilise un vocabulaire, des expressions et des tournures authentiquement québécoises
- Évite les formulations, le vocabulaire et le rythme typiques du français de France
- Marque subtilement l'oralité québécoise quand c'est naturel, sans tomber dans la caricature ou le joual forcé
- Retourne uniquement du dialogue : les paroles ou répliques d'une seule personne
- Ne décris jamais le lieu, les actions, les émotions, l'intrigue ou les autres personnages
- N'écris jamais de narration, résumé de scénario, description de scène ou texte explicatif
- N'ajoute pas de nom de personnage ni d'étiquette de locuteur : la voix doit sonner comme si elle répondait naturellement à quelqu'un
- N'utilise jamais le mot « Yo », ni comme salutation ni ailleurs dans le script
- N'inclus aucune didascalie, aucun titre, aucun numéro de ligne, aucune note entre parenthèses
- Retourne UNIQUEMENT le texte du script, rien d'autre"""

        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )

        script = text_from_anthropic_message(message)
        return jsonify({'script': script})

    except anthropic.AuthenticationError:
        return jsonify({'error': 'Clé API Anthropic invalide. Vérifiez votre fichier .env'}), 401
    except Exception as e:
        return jsonify({'error': f'Erreur lors de la génération du script : {str(e)}'}), 500


@app.route('/generate-direction', methods=['POST'])
def generate_direction():
    data = request.get_json() or {}
    scenario = (data.get('scenario') or '').strip()
    script = (data.get('script') or '').strip()
    language = normalize_language(data.get('language'))

    if not scenario and not script:
        return jsonify({'error': 'Un scénario ou un script est requis.'}), 400

    try:
        client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
        if language == 'en':
            prompt = f"""You write a short voice-performance direction for Qwen3-TTS.

Qwen follows long instructions poorly. Give one short, simple, stable sentence.

Constraints:
- Maximum 18 words.
- Describe only general tone, pace, and intensity.
- No scene-by-scene details.
- No list.
- No metaphors.
- Mention "natural English".
- Return only the direction.

Scenario:
{scenario}

Script:
{script}"""
        else:
            prompt = f"""Tu écris une micro-direction vocale pour Qwen3-TTS.

Qwen suit mal les longues directions. Donne une seule phrase courte, simple et stable.

Contraintes :
- Maximum 18 mots.
- Décris seulement le ton général, le débit et l'intensité.
- Pas de détails scène par scène.
- Pas de liste.
- Pas de métaphores.
- Mentionne "québécois naturel".
- Retourne uniquement la direction.

Scénario :
{scenario}

Script :
{script}"""

        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}]
        )
        direction = text_from_anthropic_message(message)
        return jsonify({'direction': direction})

    except anthropic.AuthenticationError:
        return jsonify({'error': 'Clé API Anthropic invalide. Vérifiez votre fichier .env'}), 401
    except Exception as e:
        return jsonify({'error': f'Erreur lors de la génération de la direction : {str(e)}'}), 500


@app.route('/transcribe-reference', methods=['POST'])
def transcribe_reference():
    audio_file = request.files.get('audio_reference')
    voice_library_id = (request.form.get('voice_library_id') or '').strip()
    language = normalize_language(request.form.get('language'))
    ref_path = None
    cleanup_ref = False

    try:
        if audio_file:
            suffix = os.path.splitext(audio_file.filename)[1] or '.wav'
            tmp_ref = named_cache_file('input', suffix)
            audio_file.save(tmp_ref.name)
            tmp_ref.close()
            ref_path = tmp_ref.name
            cleanup_ref = True
        elif voice_library_id:
            library_voice = find_voice_library_item(voice_library_id)
            if not library_voice:
                return jsonify({'error': 'Voix sauvegardée introuvable.'}), 404
            ref_path = voice_audio_path(library_voice)
        else:
            return jsonify({'error': 'Le fichier audio de référence est requis.'}), 400

        replicate_client = replicate.Client(api_token=os.getenv('REPLICATE_API_KEY'))
        model = os.getenv(
            'REPLICATE_TRANSCRIBE_MODEL',
            'openai/gpt-4o-mini-transcribe:684265b6c4d23a4f5b3536a76e0b9e022ce5084f6da95fd7d0b5ebbc573a8261'
        )

        with open(ref_path, 'rb') as ref_audio:
            output = run_transcription(replicate_client, model, ref_audio, language)

        transcript = extract_transcript(output)
        if not transcript:
            return jsonify({'error': 'La transcription est vide. Essayez un clip plus clair.'}), 500

        log_transcription(transcript)
        return jsonify({'transcript': transcript})

    except replicate.exceptions.ReplicateError as e:
        return jsonify({'error': f'Erreur Replicate transcription : {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': f'Erreur lors de la transcription : {str(e)}'}), 500
    finally:
        if cleanup_ref and ref_path and os.path.exists(ref_path):
            os.unlink(ref_path)


@app.route('/generate-audio', methods=['POST'])
def generate_audio():
    script = (request.form.get('script') or '').strip()
    ref_text = (request.form.get('ref_text') or '').strip()
    voice_direction = (request.form.get('voice_direction') or '').strip()
    audio_file = request.files.get('audio_reference')
    voice_library_id = (request.form.get('voice_library_id') or '').strip()
    language = normalize_language(request.form.get('language'))

    if not script:
        return jsonify({'error': 'Le script est requis.'}), 400

    library_voice = None
    if voice_library_id:
        library_voice = find_voice_library_item(voice_library_id)
        if not library_voice:
            return jsonify({'error': 'Voix sauvegardée introuvable.'}), 404
        if not ref_text:
            ref_text = (library_voice.get('transcript') or '').strip()

    if not ref_text:
        return jsonify({'error': 'La transcription exacte de la voix de référence est requise pour le clonage vocal.'}), 400
    if not audio_file and not library_voice:
        return jsonify({'error': 'Le fichier audio de référence est requis.'}), 400

    model = os.getenv(
        'REPLICATE_TTS_MODEL',
        'qwen/qwen3-tts:0b366549c7541af95a69454651f4ebf02c699036841cd20b78b9e2a26b4b2750'
    )

    ref_path = None
    cleanup_ref = False
    try:
        if audio_file:
            suffix = os.path.splitext(audio_file.filename)[1] or '.wav'
            tmp_ref = named_cache_file('input', suffix)
            audio_file.save(tmp_ref.name)
            tmp_ref.close()
            ref_path = tmp_ref.name
            cleanup_ref = True
        else:
            ref_path = voice_audio_path(library_voice)

        tts_script = strip_inline_stage_directions(prepare_quebec_tts_script(script, language))
        model_ref_text = normalize_tts_text(ref_text)
        log_generation_payload(model_ref_text, tts_script, voice_direction)
        replicate_client = replicate.Client(api_token=os.getenv('REPLICATE_API_KEY'))
        with open(ref_path, 'rb') as ref_audio:
            model_input = build_tts_input(model, tts_script, ref_audio, model_ref_text, voice_direction, language)

            wait_for_replicate_slot()
            output = replicate_client.run(model, input=model_input)

        # output is a URL string pointing to the generated WAV
        audio_url = str(output)
        response = requests.get(audio_url, timeout=120)
        response.raise_for_status()

        tmp_out = named_cache_file('output', '.wav')
        tmp_out.write(response.content)
        tmp_out.close()

        return send_file(
            tmp_out.name,
            mimetype='audio/wav',
            as_attachment=True,
            download_name='generation_quebec.wav'
        )

    except replicate.exceptions.ReplicateError as e:
        return jsonify({'error': f'Erreur Replicate : {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': f'Erreur lors de la génération audio : {str(e)}'}), 500
    finally:
        if cleanup_ref and ref_path and os.path.exists(ref_path):
            os.unlink(ref_path)


if __name__ == '__main__':
    host = os.getenv('HOST', '0.0.0.0')
    port = int(os.getenv('PORT', 5050))
    https_port_value = (os.getenv('HTTPS_PORT') or '').strip()
    ssl_cert = resolve_app_path(os.getenv('WALLA_SSL_CERT_FILE'))
    ssl_key = resolve_app_path(os.getenv('WALLA_SSL_KEY_FILE'))

    if https_port_value:
        if not ssl_cert or not ssl_key:
            raise RuntimeError('HTTPS_PORT requiert WALLA_SSL_CERT_FILE et WALLA_SSL_KEY_FILE.')
        if not os.path.isfile(ssl_cert) or not os.path.isfile(ssl_key):
            raise FileNotFoundError('Le certificat ou la clé HTTPS configuré est introuvable.')

        from werkzeug.serving import make_server

        https_port = int(https_port_value)
        http_server = make_server(host, port, app, threaded=True)
        https_server = make_server(
            host,
            https_port,
            app,
            threaded=True,
            ssl_context=(ssl_cert, ssl_key)
        )
        http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        http_thread.start()

        print(f"\nwalla-gen - http://{host}:{port}")
        print(f"walla-gen HTTPS - https://{host}:{https_port}")
        print("    Accessible sur votre réseau LAN\n")
        try:
            https_server.serve_forever()
        finally:
            http_server.shutdown()
            https_server.server_close()
            http_server.server_close()
    else:
        print(f"\nwalla-gen - http://{host}:{port}")
        print("    Accessible sur votre réseau LAN\n")
        app.run(host=host, port=port, debug=False)
