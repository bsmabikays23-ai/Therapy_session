import json
import os
import re
import uuid
from collections import Counter, defaultdict, namedtuple
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from flask import Flask, Response, flash, jsonify, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash
from prompts import THERAPEUTIC_SYSTEM_PROMPT, THREAD_PROMPT, LETTER_PROMPT

IMPORT_ERROR = None
try:
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
except ImportError as _err:
    IMPORT_ERROR = str(_err)
    torch = None
    AutoModelForSequenceClassification = None
    AutoTokenizer = None

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))

app = Flask(__name__)
INSTANCE_DB_PATH = os.path.join(BASE_DIR, 'instance', 'therapy.db')

app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'default-dev-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', f'sqlite:///{INSTANCE_DB_PATH}')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

GROQ_API_KEY = os.getenv('GROQ_API_KEY')
GROQ_MODEL = os.getenv('GROQ_MODEL', 'openai/gpt-oss-120b')
GROQ_MODELS = [GROQ_MODEL, 'openai/gpt-oss-20b']

BERT_DIR = (os.getenv('LOCAL_BERT_MODEL_PATH') or os.path.join(BASE_DIR, 'bert_model')).strip()
METRICS_PATH = os.path.join(BASE_DIR, 'bert_metrics.json')
MODEL_CACHE = {}

LOCAL_OFFSET = timedelta(hours=int(os.getenv('TZ_OFFSET_HOURS', '2')))

EMERGENCY_LINES = [
    {'name': 'SADAG', 'number': '0800 567 567'},
    {'name': 'Cipla Mental Health', 'number': '0800 456 789'},
    {'name': 'Lifeline', 'number': '0861 322 322'},
]

CRISIS_PATTERNS = re.compile(
    r"\bkill(ing)? myself\b|\bsuicid|\bend(ing)? (it all|my life|things)\b|\bwant to die\b|"
    r"\bdon'?t want to (be here|live|wake up|exist)\b|\bbetter off (without me|dead)\b|"
    r"\bhurt(ing)? myself\b|\bself[- ]?harm|\bcut(ting)? myself\b|\bno reason to live\b|"
    r"\btake my own life\b|\boverdos|\bcan'?t go on\b|\bwish i (was|were) dead\b",
    re.IGNORECASE,
)

CRISIS_REPLY = (
    "Thank you for telling me. I'm taking this seriously. "
    "Are you safe right now, or thinking about ending your life? "
    "If you're in danger, please call 112 or go to the nearest emergency room. "
    "You can also call SADAG on 0800 567 567 or Lifeline on 0861 322 322, any time. "
    "I'm staying right here with you."
)

CALM_TRIGGERS = re.compile(
    r"\bpanic|\banxi(ous|ety)\b|\boverwhelm|\bstress|\bcan'?t breathe\b|\bracing\b|"
    r"\bshaking\b|\bon edge\b|\bworried\b|\bnervous\b|\bwound up\b|\btense\b",
    re.IGNORECASE,
)


def is_crisis(text: str) -> bool:
    return bool(CRISIS_PATTERNS.search(text or ''))


def _norm(text: str) -> str:
    return re.sub(r'[^a-z0-9 ]', '', (text or '').lower()).strip()


def tidy(reply: str, recent_bots: List[str], user_msg: str) -> str:
    original = reply.strip().strip('"').strip()
    sentences = re.split(r'(?<=[.!?])\s+', original)

    if len(sentences) > 1:
        ratio = SequenceMatcher(None, _norm(sentences[0]), _norm(user_msg)).ratio()
        if ratio > 0.85:
            sentences = sentences[1:]

    asked_recently = any((b or '').strip().endswith('?') for b in recent_bots[-2:])
    if asked_recently and len(sentences) > 1 and sentences[-1].endswith('?'):
        trimmed = sentences[:-1]
        if len(' '.join(trimmed).split()) >= 5:
            sentences = trimmed

    cleaned = ' '.join(sentences).strip()
    return cleaned or original


def load_bert():
    if 'bert' in MODEL_CACHE:
        return MODEL_CACHE['bert']
    MODEL_CACHE['bert'] = None

    if AutoTokenizer is None or torch is None:
        MODEL_CACHE['bert_error'] = (
            f'torch/transformers not installed in this Python ({IMPORT_ERROR}). '
            'Run: pip install torch transformers'
        )
        return None
    if not os.path.isdir(BERT_DIR):
        MODEL_CACHE['bert_error'] = f'bert_model folder not found at {BERT_DIR}'
        return None

    try:
        tokenizer = AutoTokenizer.from_pretrained(BERT_DIR)
        model = AutoModelForSequenceClassification.from_pretrained(BERT_DIR)
        if torch.cuda.is_available():
            model = model.to('cuda')
        model.eval()
        MODEL_CACHE['bert'] = (tokenizer, model)
        MODEL_CACHE['bert_error'] = None
        labels = list(getattr(model.config, 'id2label', {}).values())
        print(f'[BERT] loaded from {BERT_DIR} | {len(labels)} labels, e.g. {labels[:5]}')
    except Exception as err:
        MODEL_CACHE['bert_error'] = f'{type(err).__name__}: {err}'[:240]
        print(f'[BERT ERROR] load failed: {MODEL_CACHE["bert_error"]}')
    return MODEL_CACHE['bert']


def predict_intent(text: str) -> Optional[Tuple[str, float]]:
    loaded = load_bert()
    if not loaded or not text:
        return None
    tokenizer, model = loaded
    try:
        inputs = tokenizer(text, return_tensors='pt', truncation=True, max_length=256)
        device = next(model.parameters()).device
        inputs = {k: v.to(device) for k, v in inputs.items()}
        with torch.no_grad():
            probs = torch.softmax(model(**inputs).logits, dim=-1)[0]
        idx = int(torch.argmax(probs).item())
        label = str(getattr(model.config, 'id2label', {}).get(idx, idx))
        return label, float(probs[idx].item())
    except Exception as err:
        MODEL_CACHE['bert_error'] = f'predict failed: {type(err).__name__}: {err}'[:240]
        print(f'[BERT ERROR] {MODEL_CACHE["bert_error"]}')
        return None


def load_metrics() -> Optional[Dict]:
    try:
        with open(METRICS_PATH, encoding='utf-8') as f:
            m = json.load(f)
        return {
            'accuracy': round(m['accuracy'] * 100, 1),
            'precision': round(m['precision_weighted'] * 100, 1),
            'recall': round(m['recall_weighted'] * 100, 1),
            'f1': round(m['f1_weighted'] * 100, 1),
            'average': 'weighted',
            'n_test': m.get('n_test'),
        }
    except (OSError, KeyError, ValueError):
        return None


def build_analysis(intent: Optional[Tuple[str, float]]) -> Dict:
    return {
        'intent': intent[0] if intent else None,
        'confidence': round(intent[1] * 100, 1) if intent else None,
        'metrics': load_metrics(),
        'bert_error': None if intent else (MODEL_CACHE.get('bert_error') or 'no prediction'),
    }


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    chats = db.relationship('ChatMessage', backref='user', lazy=True)
    journals = db.relationship('JournalEntry', backref='user', lazy=True)


class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    session_id = db.Column(db.String(36), nullable=False, default=lambda: str(uuid.uuid4()))
    sender = db.Column(db.String(10), nullable=False)
    message = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


class JournalEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    wins = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class MessageAnalysis(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey('chat_message.id'), unique=True, nullable=False)
    intent = db.Column(db.String(80))
    confidence = db.Column(db.Float)


class ThreadSnapshot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Letter(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    body = db.Column(db.Text, nullable=False)
    reply = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


with app.app_context():
    db.create_all()
    if not User.query.first():
        demo = User(username='Demo User', email='demo@example.com', password_hash=generate_password_hash('demo123'))
        db.session.add(demo)
        db.session.commit()


def get_local_reply(user_message: str) -> str:
    text = (user_message or '').lower().strip()
    words = set(re.findall(r"[a-z']+", text))

    if not text:
        return "I'm here. Take your time."

    if words & {'hi', 'hello', 'hey'} and len(words) <= 3:
        return "Hey. I'm glad you're here."

    if words & {'anxious', 'panic', 'overwhelmed', 'stress', 'stressed'}:
        return "That's a lot to carry at once. You don't have to sort it all out right now."

    if words & {'sad', 'lonely', 'empty', 'down', 'hurt', 'heartbroken'}:
        return "I'm sorry. That's a heavy place to be. I'm here."

    if words & {'tired', 'exhausted', 'drained'}:
        return "Yeah. That kind of tired doesn't go away with sleep."

    if 'advice' in text or 'what should i do' in text or 'how do i fix' in text:
        return "We can think it through together. What feels like the hardest part right now?"

    return "I'm here. Say it however it comes out, it doesn't have to be neat."


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password')

        if not username or not email or not password:
            flash('Please complete all fields.')
            return redirect(url_for('register'))

        if User.query.filter_by(email=email).first():
            flash('Email already registered.')
            return redirect(url_for('register'))

        user = User(username=username, email=email, password_hash=generate_password_hash(password))
        db.session.add(user)
        db.session.commit()
        flash('Account created successfully. Please log in.')
        return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password_hash, password):
            session['user_id'] = user.id
            return redirect(url_for('dashboard'))

        flash('Invalid credentials.')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('user_id', None)
    session.pop('chat_session_id', None)
    return redirect(url_for('login'))


@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


SessionPreview = namedtuple('SessionPreview', 'session_id start_time first_msg')


def build_past_sessions(user_id: int):
    rows = (
        ChatMessage.query.filter_by(user_id=user_id)
        .order_by(ChatMessage.timestamp.asc(), ChatMessage.id.asc())
        .all()
    )
    sessions = {}
    for row in rows:
        entry = sessions.setdefault(row.session_id, {'start': row.timestamp, 'first': None})
        if entry['first'] is None and row.sender == 'user':
            entry['first'] = row.message
    previews = [
        SessionPreview(sid, data['start'], data['first'] or 'New conversation')
        for sid, data in sessions.items()
    ]
    previews.sort(key=lambda p: p.start_time, reverse=True)
    return previews


@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    active_session_id = session.get('chat_session_id') or str(uuid.uuid4())
    session['chat_session_id'] = active_session_id

    history = (
        ChatMessage.query.filter_by(user_id=session['user_id'], session_id=active_session_id)
        .order_by(ChatMessage.timestamp.asc(), ChatMessage.id.asc())
        .all()
    )

    ids = [m.id for m in history]
    rows = MessageAnalysis.query.filter(MessageAnalysis.message_id.in_(ids)).all() if ids else []
    metrics = load_metrics()
    analyses = {
        r.message_id: {'intent': r.intent, 'confidence': r.confidence, 'metrics': metrics}
        for r in rows
    }

    return render_template(
        'dashboard.html',
        analyses=analyses,
        bert_metrics=metrics,
        history=history,
        past_sessions=build_past_sessions(session['user_id']),
        active_session_id=active_session_id,
        emergencies=EMERGENCY_LINES,
    )


@app.route('/chat/new')
def new_chat():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    session['chat_session_id'] = str(uuid.uuid4())
    return redirect(url_for('dashboard'))


@app.route('/chat/<session_id>')
def switch_chat(session_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    session['chat_session_id'] = session_id
    return redirect(url_for('dashboard'))


def _delete_messages(user_id: int, session_id: Optional[str] = None) -> None:
    query = ChatMessage.query.filter(ChatMessage.user_id == user_id)
    if session_id:
        query = query.filter(ChatMessage.session_id == session_id)
    ids = [m.id for m in query.all()]
    if ids:
        MessageAnalysis.query.filter(MessageAnalysis.message_id.in_(ids)).delete(synchronize_session=False)
        ChatMessage.query.filter(ChatMessage.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()


@app.route('/chat/<session_id>/delete', methods=['POST'])
def delete_chat(session_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    _delete_messages(session['user_id'], session_id)
    session['chat_session_id'] = str(uuid.uuid4())
    return redirect(url_for('dashboard'))


@app.route('/account/delete-chats', methods=['POST'])
def delete_all_chats():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    _delete_messages(session['user_id'])
    session['chat_session_id'] = str(uuid.uuid4())
    flash('All your conversations were deleted.')
    return redirect(url_for('insights'))


@app.route('/export')
def export_chats():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    rows = (
        ChatMessage.query.filter_by(user_id=session['user_id'])
        .order_by(ChatMessage.timestamp.asc(), ChatMessage.id.asc())
        .all()
    )
    grouped = {}
    for m in rows:
        grouped.setdefault(m.session_id, []).append(m)

    lines = ['Serene: your conversations', '']
    for msgs in grouped.values():
        start = msgs[0].timestamp + LOCAL_OFFSET
        lines.append(f'=== Conversation, {start:%d %b %Y %H:%M} ===')
        for m in msgs:
            who = 'You' if m.sender == 'user' else 'Serene'
            lines.append(f'{who}: {m.message}')
        lines.append('')

    return Response(
        '\n'.join(lines),
        mimetype='text/plain; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=serene-conversations.txt'},
    )


THREAD_REFRESH_DAYS = 7
THREAD_MIN_USER_MESSAGES = 5


def _thread_messages(user_id: int, limit: int = 40):
    rows = (
        db.session.query(ChatMessage, MessageAnalysis)
        .outerjoin(MessageAnalysis, MessageAnalysis.message_id == ChatMessage.id)
        .filter(ChatMessage.user_id == user_id, ChatMessage.sender == 'user')
        .order_by(ChatMessage.timestamp.asc(), ChatMessage.id.asc())
        .all()
    )
    return rows[-limit:]


def _thread_transcript(rows) -> str:
    lines = []
    for msg, analysis in rows:
        ts = (msg.timestamp + LOCAL_OFFSET).strftime('%d %b %Y')
        tag = f" [{analysis.intent}]" if analysis and analysis.intent else ""
        lines.append(f"{ts}{tag}: {msg.message}")
    return "\n".join(lines)


def generate_thread(user_id: int):
    rows = _thread_messages(user_id)
    if len(rows) < THREAD_MIN_USER_MESSAGES:
        return None

    transcript = _thread_transcript(rows)
    result = _groq_complete(
        [
            {'role': 'system', 'content': THREAD_PROMPT},
            {'role': 'user', 'content': transcript},
        ],
        max_tokens=400,
        temperature=0.6,
    )
    return result[0] if result else None


@app.route('/thread', methods=['GET'])
def thread():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    uid = session['user_id']
    latest = (
        ThreadSnapshot.query.filter_by(user_id=uid)
        .order_by(ThreadSnapshot.created_at.desc())
        .first()
    )

    needs_refresh = (
        latest is None
        or (datetime.utcnow() - latest.created_at).days >= THREAD_REFRESH_DAYS
    )

    if needs_refresh:
        fresh = generate_thread(uid)
        if fresh:
            latest = ThreadSnapshot(user_id=uid, content=fresh)
            db.session.add(latest)
            db.session.commit()

    has_enough = len(_thread_messages(uid)) >= THREAD_MIN_USER_MESSAGES

    return render_template(
        'thread.html',
        snapshot=latest,
        has_enough=has_enough,
    )


@app.route('/thread/refresh', methods=['POST'])
def thread_refresh():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    uid = session['user_id']
    fresh = generate_thread(uid)
    if fresh:
        db.session.add(ThreadSnapshot(user_id=uid, content=fresh))
        db.session.commit()
        flash('Your Thread has been updated.')
    else:
        flash("There isn't enough to reflect on yet. Talk a little more, then try again.")
    return redirect(url_for('thread'))


@app.route('/letters', methods=['GET', 'POST'])
def letters():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':
        body = (request.form.get('body') or '').strip()
        if not body:
            flash('Write something first, even a sentence.')
            return redirect(url_for('letters'))

        reply_text = None
        result = _groq_complete(
            [
                {'role': 'system', 'content': LETTER_PROMPT},
                {'role': 'user', 'content': body},
            ],
            max_tokens=250,
            temperature=0.7,
        )
        if result:
            reply_text = result[0]

        db.session.add(Letter(
            user_id=session['user_id'],
            body=body,
            reply=reply_text,
        ))
        db.session.commit()
        return redirect(url_for('letters'))

    past = (
        Letter.query.filter_by(user_id=session['user_id'])
        .order_by(Letter.created_at.desc())
        .all()
    )
    return render_template('letters.html', letters=past)


@app.route('/journal', methods=['GET', 'POST'])
def journal():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':
        wins = (request.form.get('wins') or '').strip()
        if not wins:
            flash('Please enter a journal reflection.')
            return redirect(url_for('journal'))

        db.session.add(JournalEntry(user_id=session['user_id'], wins=wins))
        db.session.commit()
        return redirect(url_for('journal'))

    entries = JournalEntry.query.filter_by(user_id=session['user_id']).order_by(JournalEntry.created_at.desc()).all()
    return render_template('journal.html', entries=entries)


def chat_streak(user_id: int, today) -> Tuple[int, int]:
    rows = (
        db.session.query(ChatMessage.timestamp)
        .filter(ChatMessage.user_id == user_id, ChatMessage.sender == 'user')
        .all()
    )
    days = {(r.timestamp + LOCAL_OFFSET).date() for r in rows}
    streak, day = 0, today
    if day not in days:
        day -= timedelta(days=1)
    while day in days:
        streak += 1
        day -= timedelta(days=1)
    return streak, len(days)


@app.route('/insights')
def insights():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    uid = session['user_id']
    today = (datetime.utcnow() + LOCAL_OFFSET).date()
    start_day = today - timedelta(days=13)
    since = datetime.combine(start_day, datetime.min.time()) - LOCAL_OFFSET

    user_msgs = (
        ChatMessage.query.filter(
            ChatMessage.user_id == uid, ChatMessage.sender == 'user', ChatMessage.timestamp >= since
        ).all()
    )
    intent_rows = (
        db.session.query(MessageAnalysis.intent, ChatMessage.timestamp)
        .join(ChatMessage, ChatMessage.id == MessageAnalysis.message_id)
        .filter(ChatMessage.user_id == uid, ChatMessage.timestamp >= since)
        .all()
    )

    counts = Counter(i for i, _ in intent_rows if i)
    total = sum(counts.values())
    most = counts.most_common(1)[0][1] if counts else 0
    top_feelings = [
        {
            'name': name.replace('_', ' '),
            'count': c,
            'share': round(100 * c / total),
            'width': max(6, round(100 * c / most)),
        }
        for name, c in counts.most_common(6)
    ]

    msgs_per_day = Counter((m.timestamp + LOCAL_OFFSET).date() for m in user_msgs)
    intents_per_day = defaultdict(Counter)
    for intent, ts in intent_rows:
        if intent:
            intents_per_day[(ts + LOCAL_OFFSET).date()][intent] += 1

    peak = max(msgs_per_day.values(), default=0)
    strip = []
    for i in range(14):
        day = start_day + timedelta(days=i)
        count = msgs_per_day.get(day, 0)
        top = intents_per_day[day].most_common(1)[0][0].replace('_', ' ') if intents_per_day.get(day) else None
        strip.append({
            'short': day.strftime('%a')[:2],
            'label': day.strftime('%a %d %b'),
            'count': count,
            'top': top,
            'height': max(4, round(100 * count / peak)) if peak else 4,
        })

    streak, days_talked = chat_streak(uid, today)
    journal_count = JournalEntry.query.filter(JournalEntry.user_id == uid, JournalEntry.created_at >= since).count()

    bert_ok = load_bert() is not None

    return render_template(
        'insights.html',
        top_feelings=top_feelings,
        strip=strip,
        stats={
            'streak': streak,
            'days_talked': days_talked,
            'messages': len(user_msgs),
            'journal': journal_count,
        },
        metrics=load_metrics(),
        bert_ok=bert_ok,
        bert_error=MODEL_CACHE.get('bert_error'),
        has_feelings=bool(top_feelings),
    )


def _groq_complete(messages, max_tokens=150, temperature=0.8):
    if not GROQ_API_KEY:
        print('[GROQ] GROQ_API_KEY is missing.')
        return None

    try:
        from openai import OpenAI
    except ImportError:
        print('[GROQ] The openai package is missing. Run: pip install openai')
        return None

    client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=GROQ_API_KEY, timeout=15, max_retries=0)

    for model in GROQ_MODELS:
        try:
            completion = client.chat.completions.create(
                model=model, messages=messages, max_tokens=max_tokens, temperature=temperature
            )
            content = completion.choices[0].message.content if completion.choices else None
            if content and content.strip():
                return content.strip(), model
            print(f'[GROQ] {model} returned an empty reply')
        except Exception as err:
            print(f'[GROQ ERROR] {model}: {type(err).__name__}: {err}')
    return None


def generate_groq_reply(history, intent_label: Optional[str] = None):
    system_content = THERAPEUTIC_SYSTEM_PROMPT
    messages = [{'role': 'system', 'content': system_content}]
    for msg in history:
        messages.append({
            'role': 'user' if msg.sender == 'user' else 'assistant',
            'content': msg.message,
        })
    return _groq_complete(messages, max_tokens=150, temperature=0.8)


SUMMARY_PROMPT = (
    "You are Serene, a warm, calm companion. Below is a conversation between Serene and a person. "
    "Write a short reflection of it, 3 to 4 sentences, speaking to the person as 'you': what you shared, "
    "the feelings underneath in plain words, and one small, gentle thing you might hold onto or try if you want to. "
    "Warm, never clinical. No diagnosis, no medication advice, no lists, no headings. "
    "If the conversation includes thoughts of suicide or self-harm, say you are glad they spoke about it and "
    "mention SADAG 0800 567 567, Lifeline 0861 322 322 and 112 for emergencies."
)


@app.route('/api/summary', methods=['POST'])
def summary():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    session_id = session.get('chat_session_id')
    msgs = (
        ChatMessage.query.filter_by(user_id=session['user_id'], session_id=session_id)
        .order_by(ChatMessage.timestamp.asc(), ChatMessage.id.asc())
        .all()
    )[-30:]

    if sum(1 for m in msgs if m.sender == 'user') < 2:
        return jsonify({'summary': "There isn't enough here to reflect on yet. Talk a little more, then try again."})

    transcript = '\n'.join(f"{'Person' if m.sender == 'user' else 'Serene'}: {m.message}" for m in msgs)
    result = _groq_complete(
        [{'role': 'system', 'content': SUMMARY_PROMPT}, {'role': 'user', 'content': transcript}],
        max_tokens=220, temperature=0.6,
    )
    if not result:
        return jsonify({'summary': None, 'error': 'The reflection is unavailable right now.'}), 503
    return jsonify({'summary': result[0]})


@app.route('/api/chat', methods=['POST'])
def chat():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json(silent=True) or {}
    user_message = (data.get('message') or '').strip()

    if not user_message:
        return jsonify({'error': 'Message is required.'}), 400

    session_id = session.get('chat_session_id') or str(uuid.uuid4())
    session['chat_session_id'] = session_id

    user_row = ChatMessage(
        user_id=session['user_id'], session_id=session_id, sender='user', message=user_message
    )
    db.session.add(user_row)
    db.session.commit()

    intent = predict_intent(user_message)
    analysis = build_analysis(intent)
    if intent:
        db.session.add(MessageAnalysis(message_id=user_row.id, intent=intent[0], confidence=intent[1] * 100))
        db.session.commit()

    history = (
        ChatMessage.query
        .filter_by(user_id=session['user_id'], session_id=session_id)
        .order_by(ChatMessage.timestamp.asc(), ChatMessage.id.asc())
        .all()
    )[-12:]

    recent_bots = [m.message for m in history if m.sender == 'bot'][-2:]

    crisis = is_crisis(user_message)

    if crisis:
        bot_reply = CRISIS_REPLY
        engine = 'crisis-fixed'
        print('[SAFETY] Crisis keywords detected, using fixed reply.')
    else:
        result = generate_groq_reply(history, intent[0] if intent else None)
        if result:
            bot_reply, model_used = result
            engine = f'groq:{model_used}'
        else:
            engine = 'keyword-fallback'
            bot_reply = get_local_reply(user_message)

        try:
            bot_reply = tidy(bot_reply, recent_bots, user_message)
        except Exception as err:
            print(f'[TIDY ERROR] {type(err).__name__}: {err}')

    db.session.add(ChatMessage(
        user_id=session['user_id'], session_id=session_id, sender='bot', message=bot_reply
    ))
    db.session.commit()

    suggest = 'calm' if (not crisis and CALM_TRIGGERS.search(user_message)) else None

    print(f'[ENGINE] {engine}')
    return jsonify({
        'reply': bot_reply, 'session_id': session_id, 'crisis': crisis, 'engine': engine,
        'user_message_id': user_row.id, 'analysis': analysis, 'suggest': suggest,
    })


@app.route('/api/health')
def health():
    if 'user_id' not in session:
        return jsonify({'error': 'Log in first'}), 401

    loaded = load_bert()
    files = sorted(os.listdir(BERT_DIR)) if os.path.isdir(BERT_DIR) else []
    labels = []
    if loaded:
        labels = list(getattr(loaded[1].config, 'id2label', {}).values())

    return jsonify({
        'groq_key_set': bool(GROQ_API_KEY),
        'groq_models': GROQ_MODELS,
        'torch_installed': torch is not None,
        'import_error': IMPORT_ERROR,
        'bert_dir': BERT_DIR,
        'bert_dir_exists': os.path.isdir(BERT_DIR),
        'bert_files': files,
        'bert_loaded': loaded is not None,
        'bert_error': MODEL_CACHE.get('bert_error'),
        'bert_label_count': len(labels),
        'bert_labels_sample': labels[:8],
        'metrics_file_exists': os.path.exists(METRICS_PATH),
        'metrics': load_metrics(),
    })


if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)