"""
Main application file for the Math Game.

This Flask application provides a simple arithmetic quiz for children. It
generates a series of random addition or subtraction questions, gives
immediate feedback on each answer, tracks the player's score for the
current session, and maintains a persistent leaderboard in a PostgreSQL
database. The database connection string should be provided via the
``DATABASE_URL`` environment variable, which is automatically set when
using the PostgreSQL plugin on Railway. A ``SECRET_KEY`` can also be
provided via environment variables for session security.

The application uses a connection pool from psycopg2 to efficiently
handle concurrent database requests and includes safeguards to
automatically recreate the pool if connections drop. It only retains
the 100 most recently active players to limit storage usage.

See templates/index.html for the HTML front‑end.
"""

import os
import random
from datetime import datetime
from typing import List, Dict, Optional, Any

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
)
import psycopg2
from psycopg2 import pool, OperationalError


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "super‑secret‑key")

# Read the database connection URL from the environment. On Railway, when you
# provision a PostgreSQL plugin, this variable is automatically injected
# into your service, as noted in the Railway docs【530127382822440†L233-L248】.
DATABASE_URL: Optional[str] = os.environ.get("DATABASE_URL")

# Connection pool will be created on demand. Using a pool helps ensure
# efficient reuse of connections when handling multiple concurrent
# requests. The pool size of (1, 10) allows up to 10 simultaneous
# connections which is sufficient for ~50 concurrent users.
_db_pool: Optional[pool.SimpleConnectionPool] = None


def get_db_pool() -> Optional[pool.SimpleConnectionPool]:
    """Initialize (if necessary) and return the global connection pool.

    Returns ``None`` if the ``DATABASE_URL`` is not set or if the pool
    cannot be created. If an OperationalError occurs during use, the
    global pool is reset so that a new one is created on the next call.
    """
    global _db_pool
    # If no database URL is configured, we cannot create a pool.
    if not DATABASE_URL:
        print("Warning: DATABASE_URL is not set. Database operations are disabled.")
        return None
    # Create the pool on first use.
    if _db_pool is None:
        try:
            # Minimum 1 connection, maximum 10 connections in the pool.
            _db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, dsn=DATABASE_URL)
        except Exception as exc:
            print(f"Error creating connection pool: {exc}")
            _db_pool = None
    return _db_pool


def execute_query(
    query: str,
    params: Optional[tuple] = None,
    *,
    fetchone: bool = False,
    fetchall: bool = False,
) -> Optional[Any]:
    """Execute a SQL query using a pooled connection.

    Parameters
    ----------
    query : str
        The SQL statement to execute.
    params : tuple or None
        Parameters to use with the SQL statement.
    fetchone : bool
        If True, return the first row of results.
    fetchall : bool
        If True, return all rows of results.

    Returns
    -------
    Any or None
        When ``fetchone`` is True, returns a single row (tuple) or None;
        when ``fetchall`` is True, returns a list of rows; otherwise returns
        None.
    """
    pool = get_db_pool()
    if pool is None:
        return None
    conn = None
    try:
        conn = pool.getconn()
        with conn.cursor() as cur:
            cur.execute(query, params)
            result = None
            if fetchone:
                result = cur.fetchone()
            elif fetchall:
                result = cur.fetchall()
            # Commit any changes (INSERT/UPDATE/DELETE)
            conn.commit()
            return result
    except OperationalError as exc:
        # Reset the pool on operational errors (e.g. network disconnects).
        global _db_pool
        _db_pool = None
        print(f"OperationalError during DB operation: {exc}")
        return None
    except Exception as exc:
        print(f"Database error: {exc}")
        if conn:
            conn.rollback()
        return None
    finally:
        if conn:
            pool.putconn(conn)


# ---------------------------------------------------------------------------
# Database management helpers
# ---------------------------------------------------------------------------


def init_db() -> None:
    """Create the players table if it does not exist.

    This function should run once when the application starts. It creates a
    table to store player names, cumulative scores, and the timestamp of
    their last game. The name column is marked UNIQUE so we can easily
    update existing records.
    """
    query = """
    CREATE TABLE IF NOT EXISTS players (
        id SERIAL PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        total_score INTEGER NOT NULL DEFAULT 0,
        last_played TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """
    execute_query(query)


def get_player_score(name: str) -> Optional[int]:
    """Retrieve the total score for a given player name.

    Returns None if the player record does not exist or if a database
    error occurs.
    """
    row = execute_query(
        "SELECT total_score FROM players WHERE name = %s;",
        (name,),
        fetchone=True,
    )
    return row[0] if row else None


def create_player(name: str) -> None:
    """Ensure a player record exists with an initial score of zero.

    Uses an UPSERT pattern so repeated calls are safe.
    """
    execute_query(
        """
        INSERT INTO players (name, total_score, last_played)
        VALUES (%s, 0, NOW())
        ON CONFLICT (name) DO NOTHING;
        """,
        (name,),
    )


def update_player_score(name: str, additional_points: int) -> None:
    """Increment a player's total score and update last_played timestamp."""
    execute_query(
        """
        UPDATE players
        SET total_score = total_score + %s,
            last_played = NOW()
        WHERE name = %s;
        """,
        (additional_points, name),
    )


def get_leaderboard(limit: int = 3) -> List[tuple]:
    """Return a list of top players sorted by total score descending.

    If multiple players have the same score, the one who played earlier
    (smaller last_played) appears first. Returns an empty list if there
    are no players or if the database is unreachable.
    """
    rows = execute_query(
        """
        SELECT name, total_score
        FROM players
        ORDER BY total_score DESC, last_played ASC
        LIMIT %s;
        """,
        (limit,),
        fetchall=True,
    )
    return rows or []


def prune_players(max_players: int = 100) -> None:
    """Delete player records beyond the most recent ``max_players`` entries.

    We retain only the most recently played 100 players by ordering on
    ``last_played DESC``. This helps limit database size while keeping
    leaderboards meaningful. If the table is smaller than ``max_players``
    rows, nothing is deleted.
    """
    execute_query(
        """
        DELETE FROM players
        WHERE id NOT IN (
            SELECT id FROM players
            ORDER BY last_played DESC
            LIMIT %s
        );
        """,
        (max_players,),
    )


# ---------------------------------------------------------------------------
# Game logic helpers
# ---------------------------------------------------------------------------


def generate_questions(count: int = 10) -> List[Dict[str, Any]]:
    """Generate a list of arithmetic questions.

    Each question contains two numbers (0–999), an operator ('+' or
    '-'), and the correct answer. For subtraction, we ensure that the
    result is non‑negative by swapping the numbers if necessary.
    """
    questions: List[Dict[str, Any]] = []
    for _ in range(count):
        op = random.choice(['+', '-'])
        if op == '+':
            a = random.randint(0, 999)
            b = random.randint(0, 999)
            answer = a + b
        else:
            a = random.randint(0, 999)
            b = random.randint(0, 999)
            if b > a:
                a, b = b, a  # swap to avoid negative results
            answer = a - b
        questions.append({'a': a, 'b': b, 'op': op, 'answer': answer})
    return questions


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------


# existing imports and app initialization…

init_db()  # call this once at import time to create the table

# remove or comment out:
# @app.before_first_request
# def before_first_request_func():
#     init_db()



@app.route('/', methods=['GET', 'POST'])
def index():
    """Landing page to capture the child's name.

    GET requests display the form for the player's name and show the
    leaderboard. POST requests process the submitted name, initialize
    session state for the game, and redirect to the game view.
    """
    leaderboard = get_leaderboard() or []
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            # Re‑render the form with an error message.
            return render_template(
                'index.html',
                page='login',
                leaderboard=leaderboard,
                error="Please enter your name."
            )
        # Save name and initialize session state.
        session['name'] = name
        session['session_score'] = 0
        session['questions'] = generate_questions(10)
        session['current_index'] = 0
        session['feedback'] = None
        session['final_score'] = 0
        # Ensure a record exists for this player
        create_player(name)
        return redirect(url_for('game'))
    # GET request
    if 'name' in session and session['name']:
        return redirect(url_for('game'))
    return render_template('index.html', page='login', leaderboard=leaderboard)


@app.route('/game', methods=['GET', 'POST'])
def game():
    """Display questions, handle answers, and show results when finished."""
    # If the user hasn't submitted their name, redirect to the landing page.
    if 'name' not in session:
        return redirect(url_for('index'))
    name: str = session['name']
    # Fetch the player's total score from the database (or 0 if missing).
    total_score: int = get_player_score(name) or 0
    leaderboard = get_leaderboard() or []
    questions: List[Dict[str, Any]] = session.get('questions', [])
    current_index: int = session.get('current_index', 0)
    feedback: Optional[str] = session.get('feedback', None)
    final_score: int = session.get('final_score', 0)
    # If this is a POST request, process the submitted answer.
    if request.method == 'POST' and questions:
        answer_text = request.form.get('answer', '').strip()
        try:
            user_answer = int(answer_text)
        except ValueError:
            user_answer = None
        # Evaluate the current question
        if current_index < len(questions):
            correct = questions[current_index]['answer']
            if user_answer is not None and user_answer == correct:
                session['session_score'] = session.get('session_score', 0) + 1
                feedback = 'Correct!'
            else:
                feedback = f"Incorrect! The correct answer was {correct}."
            # Save feedback and move to the next question
            session['feedback'] = feedback
            current_index += 1
            session['current_index'] = current_index
        # If we've answered all questions, update the player's total score
        if current_index >= len(questions):
            session_score = session.get('session_score', 0)
            update_player_score(name, session_score)
            prune_players()
            # Store final score for display and reset session state for a new game
            session['final_score'] = session_score
            session['session_score'] = 0
            session['questions'] = []
            session['current_index'] = 0
            feedback = None
            final_score = session_score
            # Refresh total_score and leaderboard
            total_score = get_player_score(name) or 0
            leaderboard = get_leaderboard() or []
            return render_template(
                'index.html',
                page='game_over',
                final_score=final_score,
                total_score=total_score,
                leaderboard=leaderboard,
            )
    # Determine if the game has already finished (GET after finishing)
    if not questions or current_index >= len(questions):
        # Show the game over screen using the stored final_score.
        return render_template(
            'index.html',
            page='game_over',
            final_score=final_score,
            total_score=total_score,
            leaderboard=leaderboard,
        )
    # Otherwise, display the next question
    question = questions[current_index]
    progress = current_index + 1
    question_count = len(questions)
    return render_template(
        'index.html',
        page='game',
        question=question,
        progress=progress,
        question_count=question_count,
        feedback=feedback,
        total_score=total_score,
        leaderboard=leaderboard,
    )


@app.route('/logout')
def logout() -> Any:
    """Clear the session and return to the landing page."""
    session.clear()
    return redirect(url_for('index'))


if __name__ == '__main__':
    # Running the app in debug mode for local development. In production,
    # Gunicorn will invoke the application callable defined above. When
    # deploying to Railway, a Procfile will instruct Gunicorn to run this
    # application using the ``DATABASE_URL`` provided via environment
    # variables【773595460775647†L428-L447】.
    app.run(debug=True)
