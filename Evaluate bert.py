"""
Score the BERT intent classifier on a labelled TEST set and save the results.

Usage:
    pip install scikit-learn
    python evaluate_bert.py test.csv

test.csv needs two columns: text,label
    - label is either the class name (as in the model's id2label) or its number
    - use data the model was NOT trained on, otherwise the numbers are meaningless

Writes bert_metrics.json, which app.py shows under each message.
"""
import csv
import json
import os
import sys
from datetime import datetime

import torch
from sklearn.metrics import accuracy_score, classification_report, precision_recall_fscore_support
from transformers import AutoModelForSequenceClassification, AutoTokenizer

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
MODEL_DIR = os.getenv('LOCAL_BERT_MODEL_PATH') or os.path.join(BASE_DIR, 'bert_model')
OUT_PATH = os.path.join(BASE_DIR, 'bert_metrics.json')


def compute_metrics(y_true, y_pred):
    """Weighted and macro averages, so you can report either honestly."""
    out = {'accuracy': float(accuracy_score(y_true, y_pred)), 'n_test': len(y_true)}
    for avg in ('weighted', 'macro'):
        p, r, f, _ = precision_recall_fscore_support(y_true, y_pred, average=avg, zero_division=0)
        out[f'precision_{avg}'] = float(p)
        out[f'recall_{avg}'] = float(r)
        out[f'f1_{avg}'] = float(f)
    return out


def to_id(label, label2id):
    label = str(label).strip()
    if label in label2id:
        return label2id[label]
    if label.isdigit():
        return int(label)
    raise ValueError(f"Label '{label}' is not in the model's labels: {sorted(label2id)}")


def main(csv_path):
    tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)
    model = AutoModelForSequenceClassification.from_pretrained(MODEL_DIR)
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    model.to(device).eval()
    label2id = model.config.label2id

    with open(csv_path, encoding='utf-8', newline='') as f:
        rows = list(csv.DictReader(f))
    texts = [r['text'] for r in rows]
    y_true = [to_id(r['label'], label2id) for r in rows]

    y_pred = []
    for i in range(0, len(texts), 32):
        batch = tokenizer(texts[i:i + 32], return_tensors='pt', truncation=True,
                          padding=True, max_length=256).to(device)
        with torch.no_grad():
            y_pred += torch.argmax(model(**batch).logits, dim=-1).tolist()

    metrics = compute_metrics(y_true, y_pred)
    metrics.update({'test_file': os.path.basename(csv_path), 'evaluated_at': datetime.now().isoformat(timespec='seconds')})

    print(classification_report(y_true, y_pred, zero_division=0))
    print(json.dumps(metrics, indent=2))
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(metrics, f, indent=2)
    print(f'Saved {OUT_PATH}')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit('Usage: python evaluate_bert.py test.csv')
    main(sys.argv[1])