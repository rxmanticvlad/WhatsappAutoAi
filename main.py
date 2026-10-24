
import os
from openai import OpenAI

api_key = os.getenv("OPENAI_API_KEY")

if not api_key:
    raise RuntimeError(
        "Missing OPENAI_API_KEY environment variable"
    )

client = OpenAI(api_key=api_key)


def generate_reply(message: str) -> str:
    """Generate a short, natural AI reply to an incoming message."""

    response = client.responses.create(
        model="gpt-4.1-mini",
        instructions=(
            "You are a friendly WhatsApp AI assistant. "
            "Reply naturally and concisely. "
            "Use the same language as the incoming message. "
            "Do not pretend to be the account owner. "
            "If a message is unclear, ask a brief question."
        ),
        input=message,
    )

    return response.output_text.strip()


if __name__ == "__main__":
    print("WhatsApp AI reply component is ready.")

    while True:
        message = input("\nTest message (or type exit): ").strip()

        if message.lower() == "exit":
            break

        if message:
            try:
                print("AI reply:", generate_reply(message))
            except Exception as error:
                print("Could not generate reply:", error)
              
