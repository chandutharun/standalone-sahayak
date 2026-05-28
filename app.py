from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

import pandas as pd
import requests
import streamlit as st
import mysql.connector
from mysql.connector import Error as MySQLError
from ollama import Client
from sqlalchemy import create_engine, text

APP_TITLE = "Standalone Sahayak"
APP_ICON = "🤖"
PAGE_LAYOUT = "wide"
REQUEST_TIMEOUT = 300

AVAILABLE_MODELS = [
    "llama3.1:8b",
    "deepseek-r1:8b",
    "gemma3:12b",
    "deepseek-r1:70b",
]
DEFAULT_MODEL = AVAILABLE_MODELS[0]

SYSTEM_PROMPT = """
You are Standalone Sahayak, a helpful internal assistant.
Developed by Tharun K.

Rules:
- Use employee context only when it is provided.
- Do not reveal raw retrieval logic.
- Do not say "I fetched this from RAG" or similar wording.
- If employee context is present, answer naturally using it.
- If employee context is absent, answer normally.
- If the question asks for a missing employee fact, say it is not available.
- Be concise, polite, and clear.
- Do not mention backend or database internals.
""".strip()

MYSQL_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS chat_history (
    id INT AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(128) NOT NULL,
    role VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    model VARCHAR(100) NOT NULL,
    sources TEXT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

EMPLOYEE_DUMMY_DATA = [
    {"full_name": "Arun Kumar", "staff_id": "APPLE1001", "department": "Hardware", "designation": "Engineer", "location": "Chennai", "email": "arun.kumar@apple.example", "extension": "201"},
    {"full_name": "Priya Nair", "staff_id": "APPLE1002", "department": "Software", "designation": "Senior Engineer", "location": "Bengaluru", "email": "priya.nair@apple.example", "extension": "202"},
    {"full_name": "Rahul Menon", "staff_id": "APPLE1003", "department": "Testing", "designation": "Lead", "location": "Chennai", "email": "rahul.menon@apple.example", "extension": "203"},
    {"full_name": "Sneha Iyer", "staff_id": "APPLE1004", "department": "HR", "designation": "Manager", "location": "Bengaluru", "email": "sneha.iyer@apple.example", "extension": "204"},
    {"full_name": "Vikram Shah", "staff_id": "APPLE1005", "department": "IT", "designation": "Analyst", "location": "Hyderabad", "email": "vikram.shah@apple.example", "extension": "205"},
]

def init_session_state() -> None:
    defaults = {
        "messages": [],
        "model": DEFAULT_MODEL,
        "session_id": f"sahayak_{int(time.time())}",
        "show_history": True,
        "history_loaded": False,
        "last_mode": "chat",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

def get_secret(name: str, default: str = "") -> str:
    try:
        return st.secrets.get(name, os.getenv(name, default))
    except Exception:
        return os.getenv(name, default)

def get_ollama_host() -> str:
    ip = get_secret("OLLAMA_SERVER_IP", "192.168.100.66")
    return f"http://{ip}:11434"

def get_mysql_config() -> Dict[str, Any]:
    return {
        "host": get_secret("MYSQL_HOST", "localhost"),
        "user": get_secret("MYSQL_USER", "sahayak"),
        "password": get_secret("MYSQL_PASSWORD", ""),
        "database": get_secret("MYSQL_DATABASE", "sahayak_db"),
    }

@st.cache_resource
def get_ollama_client(host: str) -> Client:
    return Client(host=host, timeout=REQUEST_TIMEOUT)

@st.cache_resource
def get_mysql_connection_params() -> Dict[str, Any]:
    return get_mysql_config()

@st.cache_resource
def get_sqlalchemy_engine():
    cfg = get_mysql_config()
    return create_engine(
        f"mysql+pymysql://{cfg['user']}:{cfg['password']}@{cfg['host']}/{cfg['database']}?charset=utf8mb4",
        pool_pre_ping=True,
        pool_recycle=3600,
    )

@contextmanager
def mysql_connection():
    conn = mysql.connector.connect(**get_mysql_connection_params())
    try:
        yield conn
    finally:
        conn.close()

def ensure_mysql_schema() -> None:
    with mysql_connection() as conn:
        cur = conn.cursor()
        cur.execute(MYSQL_TABLE_SQL)
        conn.commit()

def save_chat_message(role: str, content: str, model: str, sources: Optional[List[str]] = None) -> None:
    with mysql_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO chat_history (session_id, role, content, model, sources)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (st.session_state.session_id, role, content, model, "\n".join(sources) if sources else None),
        )
        conn.commit()

@st.cache_data(ttl=10)
def check_ollama_connection(host_url: str) -> Dict[str, Any]:
    try:
        response = requests.get(f"{host_url}/api/tags", timeout=5)
        return {"connected": response.ok, "message": "Connected 🟢" if response.ok else f"Status {response.status_code}"}
    except requests.RequestException:
        return {"connected": False, "message": "Host unreachable 🔴"}

@st.cache_data(ttl=10)
def load_chat_history_df(session_id: str, limit: int = 25) -> pd.DataFrame:
    engine = get_sqlalchemy_engine()
    query = text(
        """
        SELECT role, content, model, created_at, sources
        FROM chat_history
        WHERE session_id = :session_id
        ORDER BY id DESC
        LIMIT :limit
        """
    )
    return pd.read_sql_query(query, engine, params={"session_id": session_id, "limit": limit})

def load_recent_chat_pairs(session_id: str, limit_messages: int = 20) -> List[Dict[str, str]]:
    engine = get_sqlalchemy_engine()
    query = text(
        """
        SELECT role, content
        FROM chat_history
        WHERE session_id = :session_id
        ORDER BY id DESC
        LIMIT :limit
        """
    )
    rows = pd.read_sql_query(query, engine, params={"session_id": session_id, "limit": limit_messages})
    rows = rows.iloc[::-1]
    messages = []
    for _, r in rows.iterrows():
        if r["role"] in ("user", "assistant") and isinstance(r["content"], str) and r["content"].strip():
            messages.append({"role": r["role"], "content": r["content"]})
    return messages

def employee_table() -> pd.DataFrame:
    return pd.DataFrame(EMPLOYEE_DUMMY_DATA)

def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()

def is_employee_query(question: str) -> bool:
    q = question.lower()
    if re.search(r"\bapple\d+\b", q):
        return True
    keywords = [
        "staff id", "staffid", "employee", "department", "designation",
        "extension", "email", "location", "who is", "find", "lookup",
        "what is the staff id", "what is the staffid", "which department",
    ]
    return any(k in q for k in keywords)

def employee_search(question: str) -> List[Dict[str, str]]:
    df = employee_table()
    q = normalize_text(question)

    staff_match = re.search(r"\bapple\d+\b", q)
    if staff_match:
        sid = staff_match.group(0).upper()
        exact = df[df["staff_id"].str.upper() == sid]
        if not exact.empty:
            return exact.to_dict(orient="records")

    exact_name = df[df["full_name"].str.lower().apply(lambda x: x in q)]
    if not exact_name.empty:
        return exact_name.to_dict(orient="records")

    q_tokens = set(re.findall(r"[a-z0-9]+", q))
    results = []
    for _, row in df.iterrows():
        row_text = normalize_text(" ".join([str(v) for v in row.values]))
        score = sum(1 for tok in q_tokens if tok in row_text)
        if score >= 2:
            results.append(row.to_dict())
    return results[:3]

def format_employee_context(rows: List[Dict[str, str]]) -> str:
    if not rows:
        return "No employee record matched."
    parts = []
    for r in rows:
        parts.append(
            f"Name: {r['full_name']}\n"
            f"Staff ID: {r['staff_id']}\n"
            f"Department: {r['department']}\n"
            f"Designation: {r['designation']}\n"
            f"Location: {r['location']}\n"
            f"Email: {r['email']}\n"
            f"Extension: {r['extension']}"
        )
    return "\n\n".join(parts)

def build_messages(question: str) -> List[Dict[str, str]]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    history = st.session_state.messages[-12:]
    for msg in history:
        if msg["role"] in ("user", "assistant"):
            messages.append({"role": msg["role"], "content": msg["content"]})
    rows = employee_search(question) if is_employee_query(question) else []
    if rows:
        messages.append({"role": "system", "content": "Employee context:\n\n" + format_employee_context(rows)})
    messages.append({"role": "user", "content": question})
    return messages

def generate_answer(question: str, model: str, ollama_host: str) -> Dict[str, Any]:
    messages = build_messages(question)
    client = get_ollama_client(ollama_host)
    response = client.chat(model=model, messages=messages, stream=False)
    return {
        "answer": response["message"]["content"].strip(),
        "mode": "employee_rag" if is_employee_query(question) else "chat",
    }

def apply_styles() -> None:
    st.markdown(
        """
        <style>
        .main-header {
            font-size: 2.8rem;
            font-weight: 900;
            text-align: center;
            margin-bottom: 0.25rem;
            color: #c4b5fd;
            letter-spacing: 0.5px;
        }
        .subtle {
            text-align: center;
            color: #94a3b8;
            margin-bottom: 0.25rem;
        }
        .developer {
            text-align: center;
            color: #94a3b8;
            margin-bottom: 1rem;
            font-size: 0.95rem;
        }
        .glass {
            background: rgba(15, 23, 42, 0.72);
            border: 1px solid rgba(148, 163, 184, 0.16);
            border-radius: 18px;
            padding: 16px;
            box-shadow: 0 18px 40px rgba(0,0,0,0.22);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

def render_history() -> None:
    hist = load_chat_history_df(st.session_state.session_id, 25)
    if hist.empty:
        st.caption("No history yet.")
        return
    for _, row in hist.iloc[::-1].iterrows():
        with st.container():
            st.markdown(f"**{row['role'].title()}**")
            st.write(row["content"])
            st.divider()

def load_history_into_session() -> None:
    if st.session_state.history_loaded:
        return
    st.session_state.messages = load_recent_chat_pairs(st.session_state.session_id, 20)
    st.session_state.history_loaded = True

def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon=APP_ICON, layout=PAGE_LAYOUT)
    init_session_state()
    apply_styles()

    ollama_host = get_ollama_host()
    ensure_mysql_schema()
    connected = check_ollama_connection(ollama_host)
    load_history_into_session()

    with st.sidebar:
        st.markdown("## ⚙️ Configuration")
        st.markdown(f"**Ollama:** `{ollama_host}`")
        st.markdown(f"**Status:** {'🟢 Connected' if connected['connected'] else '🔴 Disconnected'}")
        st.caption(connected["message"])
        st.divider()

        st.selectbox("Choose model", AVAILABLE_MODELS, key="model")
        st.toggle("Show chat history", key="show_history")
        st.divider()

        if st.button("Clear Chat", width="stretch"):
            st.session_state.messages = []
            st.session_state.last_mode = "chat"
            st.session_state.history_loaded = True
            st.rerun()

        st.divider()
        st.markdown("### Session")
        st.code(st.session_state.session_id, language="text")
        st.caption(f"Mode: {st.session_state.last_mode}")

        if st.session_state.show_history:
            st.markdown("### Chat History")
            render_history()

    st.markdown(f'<div class="main-header">{APP_ICON} {APP_TITLE}</div>', unsafe_allow_html=True)
    st.markdown('<div class="subtle">Normal chat + internal assistant in one app.</div>', unsafe_allow_html=True)
    st.markdown('<div class="developer">Developed by Tharun K</div>', unsafe_allow_html=True)

    st.markdown('<div class="glass">', unsafe_allow_html=True)
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"], width="stretch"):
            st.markdown(msg["content"])
    st.markdown("</div>", unsafe_allow_html=True)

    if prompt := st.chat_input("Ask anything..."):
        if not connected["connected"]:
            st.error(f"Ollama is not connected at {ollama_host}")
            st.stop()

        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user", width="stretch"):
            st.markdown(prompt)

        with st.chat_message("assistant", width="stretch"):
            placeholder = st.empty()
            placeholder.markdown("Thinking...")

        try:
            result = generate_answer(prompt, st.session_state.model, ollama_host)
            answer = result["answer"]
            st.session_state.last_mode = result["mode"]
        except Exception as e:
            answer = f"Error: {e}"

        placeholder.markdown(answer)
        st.session_state.messages.append({"role": "assistant", "content": answer})

        save_chat_message("user", prompt, st.session_state.model, None)
        save_chat_message("assistant", answer, st.session_state.model, None)

        st.rerun()

if __name__ == "__main__":
    main()