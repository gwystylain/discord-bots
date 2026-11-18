# One image holds every bot; each compose service picks its bot with `command`.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bots ./bots

RUN useradd --create-home --uid 10001 bot
USER bot

# Default bot; override per service, e.g. command: ["python", "-m", "bots.other_bot"]
CMD ["python", "-m", "bots.ski_brief"]
