// Fixed Microphone Web Speech API Logic
let recognition = null;

if ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    recognition = new SpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.lang = 'en-US';

    recognition.onresult = (event) => {
        const transcript = event.results[0][0].transcript;
        const input = document.getElementById('userInput');
        if (input) {
            input.value = transcript;
        }
    };

    recognition.onerror = (event) => {
        console.error("Speech recognition error:", event.error);
        const micBtn = document.getElementById('micBtn');
        if (micBtn) micBtn.classList.remove('recording');
    };

    recognition.onend = () => {
        const micBtn = document.getElementById('micBtn');
        if (micBtn) micBtn.classList.remove('recording');
    };
}

function toggleVoice() {
    const micBtn = document.getElementById('micBtn');
    if (!recognition) {
        alert("Speech recognition is not supported in this browser. Please use Google Chrome or Microsoft Edge.");
        return;
    }

    if (micBtn.classList.contains('recording')) {
        recognition.stop();
        micBtn.classList.remove('recording');
    } else {
        try {
            recognition.start();
            micBtn.classList.add('recording');
        } catch (err) {
            console.error("Mic start error:", err);
        }
    }
}

// Toggle Private Diary Visibility
function toggleDiary() {
    const diaryContent = document.getElementById('diaryContent');
    const toggleBtn = document.getElementById('diaryToggleBtn');

    if (!diaryContent) return;

    if (diaryContent.classList.contains('active')) {
        diaryContent.classList.remove('active');
        toggleBtn.innerText = 'Show';
    } else {
        diaryContent.classList.add('active');
        toggleBtn.innerText = 'Hide';
    }
}

// Notification Banner Controls
function closeAffirmation() {
    const toast = document.getElementById('affirmationToast');
    if (toast) toast.style.display = 'none';
}

function showAffirmation() {
    const toast = document.getElementById('affirmationToast');
    if (toast) {
        toast.style.display = 'flex';
        toast.style.animation = 'none';
        toast.offsetHeight;
        toast.style.animation = 'slideIn 0.3s ease-out';
    }
}

// Chat API Communication
async function sendMessage() {
    const input = document.getElementById('userInput');
    const message = input.value.trim();
    if (!message) return;

    const chatBox = document.getElementById('chatBox');

    const wrapper = document.createElement('div');
    wrapper.className = 'msg-wrapper user';
    const userDiv = document.createElement('div');
    userDiv.className = 'msg user';
    userDiv.innerText = message;
    wrapper.appendChild(userDiv);
    chatBox.appendChild(wrapper);

    input.value = '';
    chatBox.scrollTop = chatBox.scrollHeight;

    try {
        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: message })
        });
        const data = await response.json();

        const botWrapper = document.createElement('div');
        botWrapper.className = 'msg-wrapper bot';

        const botDiv = document.createElement('div');
        botDiv.className = 'msg bot';
        botDiv.innerText = data.reply;

        botWrapper.appendChild(botDiv);
        chatBox.appendChild(botWrapper);
        chatBox.scrollTop = chatBox.scrollHeight;
    } catch (err) {
        console.error("Chat error:", err);
    }
}