from flask import Flask, jsonify, request, g
import os
import time
import mysql.connector
from prometheus_flask_exporter import PrometheusMetrics
from prometheus_client import Histogram

app = Flask(__name__)

metrics = PrometheusMetrics(
    app,
    group_by="endpoint"
)

request_latency = Histogram(
    "app_request_latency_seconds",
    "Application request latency in seconds",
    ["method", "endpoint", "status"]
)

DB_HOST = os.getenv("DB_HOST", "db01")
DB_NAME = os.getenv("DB_NAME", "taskdb")
DB_USER = os.getenv("DB_USER", "taskuser")
DB_PASSWORD = os.getenv("DB_PASSWORD", "taskpassword")


@app.before_request
def start_timer():
    g.start_time = time.perf_counter()


@app.after_request
def record_latency(response):
    if hasattr(g, "start_time"):
        latency = time.perf_counter() - g.start_time
        request_latency.labels(
            request.method,
            request.path,
            response.status_code
        ).observe(latency)

    return response


def get_db():
    return mysql.connector.connect(
        host=DB_HOST,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


@app.route("/health")
def health():
    return jsonify({
        "status": "healthy",
        "hostname": os.uname().nodename
    })


@app.route("/api/tasks", methods=["GET"])
def get_tasks():
    connection = get_db()
    cursor = connection.cursor(dictionary=True)
    cursor.execute("SELECT id, title, completed FROM tasks ORDER BY id")
    tasks = cursor.fetchall()
    cursor.close()
    connection.close()
    return jsonify(tasks)


@app.route("/api/tasks", methods=["POST"])
def create_task():
    data = request.get_json()
    connection = get_db()
    cursor = connection.cursor()
    cursor.execute(
        "INSERT INTO tasks (title, completed) VALUES (%s, %s)",
        (data["title"], False)
    )
    connection.commit()
    cursor.close()
    connection.close()
    return jsonify({"message": "Task created"}), 201


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
