"""
actions/utilities.py — Gama Utility Functions
===============================================
Useful everyday commands:
  - translate: translate text between languages
  - convert: unit conversion (length, weight, temp, etc.)
  - currency: currency conversion via Gemini
  - joke: tell a random joke
  - quote: inspirational quote
  - fact: random interesting fact
  - calculate: math evaluation
  - spell: spell out a word
  - define: dictionary definition via Gemini

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

from utils.paths import get_base_dir as _get_base_dir

import json
import logging
import random
import sys
from pathlib import Path

log = get_logger(__name__)
logger = log  # back-compat alias
BASE_DIR = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_api_key() -> str:
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""


def _gemini_generate(prompt: str) -> str:
    """Call Gemini with a text prompt and return the response."""
    try:
        from google import genai
        client = genai.Client(api_key=_get_api_key())
        response = client.models.generate_content(
            model="gemini-3.5-flash-lite", contents=prompt,
        )
        return (response.text or "").strip()
    except Exception as exc:
        return f"Error: {exc}"


def utilities(action: str = "joke", **kwargs) -> str:
    """Main entry point for utility functions."""
    action = (action or "joke").lower().strip()

    if action == "translate":
        return _translate(kwargs.get("text", ""), kwargs.get("from_lang", ""),
                          kwargs.get("to_lang", ""))
    if action == "convert":
        return _convert(kwargs.get("value", ""), kwargs.get("from_unit", ""),
                        kwargs.get("to_unit", ""))
    if action == "currency":
        return _currency(kwargs.get("amount", "1"), kwargs.get("from_currency", "USD"),
                         kwargs.get("to_currency", "INR"))
    if action == "joke":
        return _joke()
    if action == "quote":
        return _quote()
    if action == "fact":
        return _fact()
    if action == "calculate":
        return _calculate(kwargs.get("expression", ""))
    if action == "spell":
        return _spell(kwargs.get("word", ""))
    if action == "define":
        return _define(kwargs.get("word", ""))
    return f"Unknown utility action: {action}. Use: translate, convert, currency, joke, quote, fact, calculate, spell, define."


# ============================================================
# Translation
# ============================================================
def _translate(text: str, from_lang: str = "", to_lang: str = "English") -> str:
    text = (text or "").strip()
    if not text:
        return "What text should I translate?"
    to_lang = (to_lang or "English").strip()
    prompt = f"Translate this text to {to_lang}. Output ONLY the translation, no explanation:\n\n{text}"
    result = _gemini_generate(prompt)
    return f"Translation to {to_lang}:\n{result}"


# ============================================================
# Unit conversion
# ============================================================
def _convert(value: str, from_unit: str, to_unit: str) -> str:
    value = (value or "").strip()
    from_unit = (from_unit or "").strip()
    to_unit = (to_unit or "").strip()
    if not value or not from_unit or not to_unit:
        return "Please provide value, from_unit, and to_unit. Example: convert 5 km to miles."
    prompt = (f"Convert {value} {from_unit} to {to_unit}. "
              f"Output ONLY the result as 'X {to_unit}', no explanation.")
    result = _gemini_generate(prompt)
    return f"{value} {from_unit} = {result}"


# ============================================================
# Currency conversion
# ============================================================
def _currency(amount: str, from_currency: str = "USD", to_currency: str = "INR") -> str:
    amount = (amount or "1").strip()
    from_currency = (from_currency or "USD").upper().strip()
    to_currency = (to_currency or "INR").upper().strip()
    prompt = (f"What is the current exchange rate? Convert {amount} {from_currency} to {to_currency}. "
              f"Use real-time rates. Output the result as 'X {to_currency}'.")
    try:
        from google import genai
        client = genai.Client(api_key=_get_api_key())
        response = client.models.generate_content(
            model="gemini-3.5-flash-lite", contents=prompt,
            config={"tools": [{"google_search": {}}]},
        )
        text = ""
        for part in response.candidates[0].content.parts:
            if hasattr(part, "text") and part.text:
                text += part.text
        return f"{amount} {from_currency} = {text.strip()}"
    except Exception as exc:
        return f"Currency conversion failed: {exc}"


# ============================================================
# Jokes
# ============================================================
_JOKES = [
    "Why do programmers prefer dark mode? Because light attracts bugs!",
    "Why did the Python developer quit? Because he didn't get arrays.",
    "What do you call a programmer from Finland? Nerdic.",
    "Why do Java developers wear glasses? Because they don't C#.",
    "How many programmers does it take to change a light bulb? None — that's a hardware problem.",
    "Why did the developer go broke? Because he used up all his cache.",
    "What's a programmer's favorite hangout place? The Foo Bar.",
    "Why do programmers mix up Halloween and Christmas? Because Oct 31 == Dec 25.",
    "What did the router say to the doctor? It hurts when IP.",
    "Why was the JavaScript developer sad? Because he didn't 'null' how to express his feelings.",
    "Doctor, Doctor! I think I'm a pair of curtains. Pull yourself together!",
    "Why don't scientists trust atoms? Because they make up everything.",
    "I told my computer I needed a break, and it said 'No problem — I'll go to sleep.'",
    "Why did the function return early? Because it lost its callback.",
    "A SQL query walks into a bar, walks up to two tables and asks: 'Can I join you?'",
]


def _joke() -> str:
    return random.choice(_JOKES)


# ============================================================
# Quotes
# ============================================================
_QUOTES = [
    "The only way to do great work is to love what you do. — Steve Jobs",
    "Innovation distinguishes between a leader and a follower. — Steve Jobs",
    "The future belongs to those who believe in the beauty of their dreams. — Eleanor Roosevelt",
    "Success is not final, failure is not fatal: it is the courage to continue that counts. — Winston Churchill",
    "The only limit to our realization of tomorrow is our doubts of today. — Franklin D. Roosevelt",
    "Do what you can, with what you have, where you are. — Theodore Roosevelt",
    "Believe you can and you're halfway there. — Theodore Roosevelt",
    "It always seems impossible until it's done. — Nelson Mandela",
    "The best time to plant a tree was 20 years ago. The second best time is now. — Chinese Proverb",
    "Your time is limited, so don't waste it living someone else's life. — Steve Jobs",
    "Stay hungry, stay foolish. — Steve Jobs",
    "Code is like humor. When you have to explain it, it's bad. — Cory House",
    "The best error message is the one that never shows up. — Thomas Fuchs",
    "Simplicity is the soul of efficiency. — Austin Freeman",
    "Make it work, make it right, make it fast. — Kent Beck",
]


def _quote() -> str:
    return random.choice(_QUOTES)


# ============================================================
# Facts
# ============================================================
_FACTS = [
    "Honey never spoils. Archaeologists have found 3000-year-old honey in Egyptian tombs that's still edible!",
    "A day on Venus is longer than a year on Venus. Venus rotates so slowly that its year (225 Earth days) is shorter than its day (243 Earth days).",
    "Octopuses have three hearts, nine brains, and blue blood.",
    "A group of flamingos is called a 'flamboyance'.",
    "The first computer bug was an actual moth found in a Harvard Mark II computer in 1947.",
    "Bananas are berries, but strawberries aren't.",
    "The human brain uses about 20% of your body's total energy.",
    "Sharks existed before trees. Sharks have been around for 400 million years; trees for 350 million.",
    "A single cloud can weigh over a million pounds.",
    "The shortest war in history was between Britain and Zanzibar in 1896 — it lasted 38 minutes.",
    "Python was named after Monty Python's Flying Circus, not the snake.",
    "The first-ever webcam was invented at Cambridge University to monitor a coffee pot.",
    "There are more possible chess games than atoms in the observable universe.",
    "The average person walks about 100,000 miles in their lifetime — that's 4 times around the Earth.",
    "Wombat poop is cube-shaped.",
]


def _fact() -> str:
    return random.choice(_FACTS)


# ============================================================
# Calculator — safe AST-based evaluator (no eval())
# ============================================================
def _safe_eval(node):
    """Recursively evaluate a parsed AST math expression.
    Supports: numbers, unary +/-, binary +/-/*//, **, %, and grouping.
    Raises ValueError for anything else (function calls, names, etc.)."""
    import ast
    import math as _math
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"Unsupported literal: {node.value!r}")
    if isinstance(node, ast.UnaryOp):
        operand = _safe_eval(node.operand)
        if isinstance(node.op, ast.UAdd):
            return +operand
        if isinstance(node.op, ast.USub):
            return -operand
        raise ValueError(f"Unsupported unary op: {type(node.op).__name__}")
    if isinstance(node, ast.BinOp):
        left = _safe_eval(node.left)
        right = _safe_eval(node.right)
        ops = {
            ast.Add: lambda a, b: a + b,
            ast.Sub: lambda a, b: a - b,
            ast.Mult: lambda a, b: a * b,
            ast.Div: lambda a, b: a / b,
            ast.FloorDiv: lambda a, b: a // b,
            ast.Mod: lambda a, b: a % b,
            ast.Pow: lambda a, b: a ** b,
        }
        op_fn = ops.get(type(node.op))
        if op_fn is None:
            raise ValueError(f"Unsupported binary op: {type(node.op).__name__}")
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("Exponent too large (> 100) — refusing to compute.")
        return op_fn(left, right)
    raise ValueError(f"Unsupported expression node: {type(node).__name__}")


def _calculate(expression: str) -> str:
    import ast
    expression = (expression or "").strip()
    if not expression:
        return "What should I calculate?"
    try:
        tree = ast.parse(expression, mode="eval")
        result = _safe_eval(tree.body)
        # Format: no trailing .0 for integers
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        return f"{expression} = {result}"
    except ZeroDivisionError:
        return "Division by zero."
    except (ValueError, TypeError, SyntaxError):
        # Fallback to Gemini for complex/natural-language expressions
        result = _gemini_generate(f"Calculate this and give ONLY the numeric result: {expression}")
        return f"{expression} = {result}"
    except Exception:
        result = _gemini_generate(f"Calculate this and give ONLY the numeric result: {expression}")
        return f"{expression} = {result}"


# ============================================================
# Spell
# ============================================================
def _spell(word: str) -> str:
    word = (word or "").strip()
    if not word:
        return "What word should I spell?"
    spelled = " ".join(word.upper())
    return f"{word} is spelled: {spelled}"


# ============================================================
# Define
# ============================================================
def _define(word: str) -> str:
    word = (word or "").strip()
    if not word:
        return "What word should I define?"
    result = _gemini_generate(
        f"Define the word '{word}' in simple terms. Include part of speech and an example sentence."
    )
    return f"{word}:\n{result}"


__all__ = ["utilities"]
