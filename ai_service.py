
import os
from flask import Flask, jsonify, request
from openai import OpenAI

app = Flask(__name__)

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

@app.post("/reply")
def reply():
    data = request.get_json(silent=True) or {}
    message = data.get("message", "")

    if not isinstance(message, str) or not message.strip():
        return jsonify({"error": "Message is required"}), 400

    try:
        response = client.responses.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
            input=message.strip()
        )

        return jsonify({"reply": response.output_text})
    except Exception:
        app.logger.exception("AI reply generation failed")
        return jsonify({"error": "AI reply generation failed"}), 500

if __name__ == "__main__":
    # Internal service only; do not expose this port publicly.
    app.run(host="127.0.0.1", port=5001)
  
