FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
# No progress bar: its box-drawing characters crash the Azure CLI on Windows while it streams the cloud-build log
RUN pip install --no-cache-dir --progress-bar off -r requirements.txt

COPY memebank ./memebank
COPY scripts ./scripts

RUN useradd --system --uid 10001 bank && chown -R bank /app
USER bank

# Socket Mode: the bot connects out to Slack, so no port is exposed.
CMD ["python", "-m", "memebank"]
