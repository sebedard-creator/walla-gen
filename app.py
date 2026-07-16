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
            prompt = f"""You are an experienced scriptwriter for radio and television.

Primary objective: the text will be read by a text-to-speech / voice-cloning system.
It must therefore be natural, concise, easy to speak, and written in clear English.

Generate a natural English script for the following scenario:
"{scenario}"

The script should last approximately {duration} seconds when read aloud at a natural broadcast pace.
For voice cloning, keep it concise: short sentences, clear punctuation, and natural rhythm.

Strict rules:
- Write only in English.
- Use natural spoken English, not translated-sounding French syntax.
- Do not include stage directions, titles, line numbers, notes, or parenthetical instructions.
- Return ONLY the script text, nothing else."""
        else:
            prompt = f"""Tu es un rédacteur chevronné pour la radio et la télévision québécoise.

Objectif prioritaire : le texte sera lu ensuite par un système de synthèse vocale.
Il doit donc aider à produire une diction et une prosodie clairement québécoises,
avec un accent québécois naturel et crédible, et non un accent français de France.

Génère un script naturel et authentique en français québécois pour le scénario suivant :
"{scenario}"

Le script doit durer approximativement {duration} secondes lorsque lu à voix haute au rythme naturel d'un lecteur ou d'une lectrice de nouvelles québécois(e).
Pour le clonage vocal, reste concis : phrases courtes, ponctuation claire, débit naturel.

Règles strictes :
- Utilise un vocabulaire, des expressions et des tournures authentiquement québécoises
- Évite les formulations, le vocabulaire et le rythme typiques du français de France
- Marque subtilement l'oralité québécoise quand c'est naturel, sans tomber dans la caricature ou le joual forcé
- Le ton doit sonner naturel, comme on entend à la radio ou à la télé québécoise
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
