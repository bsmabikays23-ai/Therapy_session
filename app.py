import os
import uuid
from datetime import datetime

import requests
from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash
from huggingface_hub import InferenceClient
from prompts import THERAPEUTIC_SYSTEM_PROMPT

load_dotenv()

app = Flask(__name__)
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DB_PATH = os.path.join(BASE_DIR, 'instance', 'therapy.db')

app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'default-dev-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', f'sqlite:///{INSTANCE_DB_PATH}')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False


db = SQLAlchemy(app)

HF_API_KEY = os.getenv('HF_API_KEY')
HF_MODEL_URL = os.getenv('HF_MODEL_URL', 'https://api-inference.huggingface.co/models/mistralai/Mistral-7B-Instruct-v0.2')

EMERGENCY_LINES = [
    {'name': 'SADAG', 'number': '0800 567 567'},
    {'name': 'Cipla Mental Health', 'number': '0800 456 789'},
    {'name': 'Lifeline', 'number': '0861 322 322'},
]


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    chats = db.relationship('ChatMessage', backref='user', lazy=True)
    journals = db.relationship('JournalEntry', backref='user', lazy=True)
    quizzes = db.relationship('QuizResult', backref='user', lazy=True)


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


class QuizResult(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    total_score = db.Column(db.Integer, nullable=False)
    status_label = db.Column(db.String(50), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


with app.app_context():
    db.create_all()
    if not User.query.first():
        demo = User(username='Demo User', email='demo@example.com', password_hash=generate_password_hash('demo123'))
        db.session.add(demo)
        db.session.commit()


def get_local_reply(user_message: str) -> str:
    """Fallback only used when the Hugging Face model is unavailable."""
    text = (user_message or '').lower().strip()

    if not text:
        return "I'm here. What's on your mind?"

    if text.startswith(('hi', 'hello', 'hey')):
        return "Hey 🙂 What's going on?"

    if any(word in text for word in ['anxious', 'panic', 'overwhelmed', 'stress', 'stressed']):
        return (
            "That sounds like a lot to be carrying at once. "
            "You don't have to sort everything out right now."
        )

    if any(word in text for word in ['sad', 'lonely', 'empty', 'down', 'hurt', 'heartbroken']):
        return (
            "That sounds really painful. "
            "Take your time — I'm here to hear what happened."
        )

    if any(word in text for word in ['tired', 'exhausted', 'drained']):
        return (
            "It sounds like you're running on empty. "
            "What's been weighing on you the most?"
        )

    if any(word in text for word in ['advice', 'what should i do', 'how do i fix']):
        return (
            "Yeah, we can think through it together. "
            "What feels like the hardest part of the situation right now?"
        )

    return (
        "I hear you. Tell me what's going on in your own words — "
        "you don't have to make it sound perfect."
    )



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


@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    active_session_id = session.get('chat_session_id') or str(uuid.uuid4())
    session['chat_session_id'] = active_session_id

    history = (
        ChatMessage.query.filter_by(user_id=session['user_id'], session_id=active_session_id)
        .order_by(ChatMessage.timestamp.asc())
        .all()
    )

    past_sessions = (
        db.session.query(
            ChatMessage.session_id.label('session_id'),
            db.func.min(ChatMessage.timestamp).label('start_time'),
            db.func.min(ChatMessage.message).label('first_msg'),
        )
        .filter(ChatMessage.user_id == session['user_id'])
        .group_by(ChatMessage.session_id)
        .order_by(db.func.min(ChatMessage.timestamp).desc())
        .all()
    )

    journal = (
        JournalEntry.query.filter_by(user_id=session['user_id'])
        .order_by(JournalEntry.created_at.desc())
        .first()
    )

    return render_template(
        'dashboard.html',
        history=history,
        past_sessions=past_sessions,
        active_session_id=active_session_id,
        journal=journal,
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


@app.route('/journal', methods=['GET', 'POST'])
def journal():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':
        wins = (request.form.get('wins') or '').strip()
        if not wins:
            flash('Please enter a journal reflection.')
            return redirect(url_for('journal'))

        entry = JournalEntry(user_id=session['user_id'], wins=wins)
        db.session.add(entry)
        db.session.commit()
        return redirect(url_for('journal'))

    entries = JournalEntry.query.filter_by(user_id=session['user_id']).order_by(JournalEntry.created_at.desc()).all()
    return render_template('journal.html', entries=entries)


@app.route('/quiz', methods=['GET', 'POST'])
def quiz():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':
        answers = []
        for i in range(1, 5):
            value = request.form.get(f'q{i}', type=int)
            answers.append(value if value is not None else 0)

        total_score = sum(answers)
        if total_score <= 6:
            status_label = 'Needs gentle support'
        elif total_score <= 12:
            status_label = 'Steady but stretched'
        elif total_score <= 16:
            status_label = 'Doing better'
        else:
            status_label = 'Feeling more grounded'

        result = QuizResult(user_id=session['user_id'], total_score=total_score, status_label=status_label)
        db.session.add(result)
        db.session.commit()
        flash('Your check-in has been logged.')
        return redirect(url_for('quiz'))

    results = QuizResult.query.filter_by(user_id=session['user_id']).order_by(QuizResult.created_at.desc()).all()
    days_left = max(0, 5 - len(results))
    return render_template('quiz.html', results=results, days_left=days_left)


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

    # Save the user's message.
    db.session.add(ChatMessage(
        user_id=session['user_id'],
        session_id=session_id,
        sender='user',
        message=user_message
    ))
    db.session.commit()

    # Load recent messages so Serene has actual conversational memory.
    history = (
        ChatMessage.query
        .filter_by(
            user_id=session['user_id'],
            session_id=session_id
        )
        .order_by(ChatMessage.timestamp.asc(), ChatMessage.id.asc())
        .all()
    )[-12:]

    messages = [
        {
            'role': 'system',
            'content': THERAPEUTIC_SYSTEM_PROMPT
        }
    ]

    for msg in history:
        messages.append({
            'role': 'user' if msg.sender == 'user' else 'assistant',
            'content': msg.message
        })

    bot_reply = None
    hf_token = os.getenv('HF_API_KEY')

    if hf_token:
        try:
            # Hugging Face's current chat-completions interface.
            # Qwen2.5-7B-Instruct is an instruction/chat model with an
            # inference-provider route currently shown on its model page.
            client = InferenceClient(
                api_key=hf_token,
                provider='auto'
            )

            completion = client.chat.completions.create(
                model='Qwen/Qwen2.5-7B-Instruct',
                messages=messages,
                max_tokens=220,
                temperature=0.75,
                top_p=0.9
            )

            if completion.choices:
                content = completion.choices[0].message.content

                if content:
                    bot_reply = content.strip()

            print("[HF] AI response received successfully.")

        except Exception as err:
            # IMPORTANT: log the real Hugging Face error instead of hiding it.
            print(f"[HF ERROR] {type(err).__name__}: {err}")

    if not bot_reply:
        bot_reply = get_local_reply(user_message)
        print("[HF] Using local fallback response.")

    # Save Serene's response.
    db.session.add(ChatMessage(
        user_id=session['user_id'],
        session_id=session_id,
        sender='bot',
        message=bot_reply
    ))
    db.session.commit()

    return jsonify({
        'reply': bot_reply,
        'session_id': session_id
    })



if __name__ == '__main__':
    app.run(debug=True)