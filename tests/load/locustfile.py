"""Load test for ARTHUR (Phase 22) - simulated users sending requests at the same time.

Run it against a SEPARATE test server (never your real ARTHUR), started with the echo
model so the test measures ARTHUR's own code and not the GPU:

    python scripts/run_load_server.py                      # terminal 1: test server on :8001
    locust -f tests/load/locustfile.py --headless -u 20 -r 5 -t 30s --host http://127.0.0.1:8001

    -u 20   20 simulated users      -r 5   start 5 new users per second      -t 30s  duration

Locust vocabulary:
    User       one simulated person; runs tasks in a loop with a short pause between them
    @task(n)   something a user does; n is its weight (how often compared to the others)
    RPS        requests per second the server answered
    p95        95 % of requests were faster than this
"""

import os
import random
import uuid

from locust import HttpUser, between, constant, task

# ARTHUR_LOAD_NO_WAIT=1: users fire as fast as they can (stress test: where is the ceiling?)
STRESS = os.environ.get("ARTHUR_LOAD_NO_WAIT") == "1"


def pause(low: float, high: float):
    return constant(0) if STRESS else between(low, high)


QUESTIONS = [
    "Hello",
    "Explain what a vector database is in one sentence.",
    "Give me a tip for focused work.",
    "What is a good name for a cat?",
    "Summarise the plot of Hamlet in a sentence.",
]


class ChatUser(HttpUser):
    """Someone chatting: mostly messages, sometimes opening a panel."""

    weight = 3
    wait_time = pause(0.5, 2.0)

    def on_start(self) -> None:
        self.session_id = uuid.uuid4().hex  # each user has their own conversation

    @task(6)
    def chat(self) -> None:
        payload = {"message": random.choice(QUESTIONS), "session_id": self.session_id}
        with self.client.post("/chat", json=payload, catch_response=True) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}: {response.text[:120]}")
            elif not response.json().get("response"):
                response.failure("empty answer")

    @task(1)
    def open_memory_panel(self) -> None:
        self.client.get("/memories")

    @task(1)
    def open_reminders_panel(self) -> None:
        self.client.get("/reminders")


class DashboardUser(HttpUser):
    """The stats page and health checks polling in the background."""

    weight = 1
    wait_time = pause(1.0, 3.0)

    @task(3)
    def stats(self) -> None:
        self.client.get("/metrics/summary")

    @task(2)
    def health(self) -> None:
        self.client.get("/health")

    @task(1)
    def prometheus_scrape(self) -> None:
        self.client.get("/metrics")

    @task(1)
    def page(self) -> None:
        self.client.get("/", name="/ (page)")


class ReminderUser(HttpUser):
    """Writes to the database: add a reminder, then cancel it again."""

    weight = 1
    wait_time = pause(1.0, 3.0)

    @task
    def add_and_cancel(self) -> None:
        body = {"text": f"load test {uuid.uuid4().hex[:6]}", "when": "in 2 hours"}
        with self.client.post("/reminders", json=body, catch_response=True) as response:
            if response.status_code != 201:
                response.failure(f"HTTP {response.status_code}: {response.text[:120]}")
                return
            reminder_id = response.json()["id"]
        self.client.delete(f"/reminders/{reminder_id}", name="/reminders/{id}")
