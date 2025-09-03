# app.py (Main AshApp with dynamic deployer forwarding)
import boto3
import subprocess
import json
import os
import shutil
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, session, redirect, Response
import requests
from urllib.parse import urljoin
import logging

# --------------------------
# FLASK APP
# --------------------------
app = Flask(__name__, static_folder="frontend", static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "supersecretkey")

# --------------------------
# ENSURE AWS CLI INSTALLED
# --------------------------
def ensure_aws_cli():
    aws_path = shutil.which("aws")
    if aws_path is None:
        print("AWS CLI not found. Installing...")
        try:
            subprocess.run([
                "curl", "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip", "-o", "awscliv2.zip"
            ], check=True)
            subprocess.run(["unzip", "-o", "awscliv2.zip"], check=True)
            subprocess.run(["/tmp/aws/install"], check=True)
            aws_path = shutil.which("aws")
            print(f"AWS CLI installed successfully at {aws_path}")
        except subprocess.CalledProcessError as e:
            print("Failed to install AWS CLI:", e)
            aws_path = None
    else:
        print(f"AWS CLI already installed at {aws_path}")
    return aws_path

AWS_CLI_PATH = ensure_aws_cli()

# --------------------------
# SERVICE URLS
# --------------------------
SPRING_BOOT_URL = os.environ.get("SPRING_BOOT_URL", "http://history-service:8081")
MONITOR_URL = os.environ.get("MONITOR_URL", "http://flask-monitor:6000/monitor/log")
DEPLOYER_URL = os.environ.get("DEPLOYER_URL", "http://deployer-app:5000")  # dynamic deployer service

# --------------------------
# MONITORING LOG
# --------------------------
def log_to_monitor(user_id, service, endpoint, action_type, request_data, response_summary):
    try:
        # Mask sensitive info
        if isinstance(request_data, dict):
            for key in ["access_key", "secret_key", "token", "password"]:
                if key in request_data:
                    request_data[key] = "****"

        payload = {
            "user_id": user_id,
            "service": service,
            "endpoint": endpoint,
            "action_type": action_type,
            "request_data": request_data,
            "response_summary": response_summary,
            "ip_address": request.remote_addr if request else "N/A",
            "user_agent": request.headers.get("User-Agent", "") if request else "N/A"
        }

        resp = requests.post(MONITOR_URL, json=payload, timeout=3)
        if resp.status_code != 200:
            logging.warning(f"Monitor logging failed ({resp.status_code}): {resp.text}")

    except Exception as e:
        logging.error(f"Exception while logging to monitor: {e}")

# --------------------------
# HISTORY FUNCTIONS
# --------------------------
def save_to_history(query, output):
    payload = {"query": query, "output": output}
    try:
        response = requests.post(f"{SPRING_BOOT_URL}/history/save", json=payload)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print("Error saving history:", e)
        return None

def get_history():
    try:
        response = requests.get(f"{SPRING_BOOT_URL}/history/list")
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print("Error fetching history:", e)
        return []

# --------------------------
# AWS BEDROCK CLIENT
# --------------------------
def get_bedrock_client():
    if "aws_access_key" not in session or "aws_secret_key" not in session:
        return None
    return boto3.client(
        "bedrock-runtime",
        region_name=session.get("aws_region", "us-east-1"),
        aws_access_key_id=session["aws_access_key"],
        aws_secret_access_key=session["aws_secret_key"],
    )

def ask_bedrock(prompt):
    bedrock_runtime = get_bedrock_client()
    if not bedrock_runtime:
        return "Not logged in to AWS."
    try:
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 500,
            "temperature": 0.7,
        }
        response = bedrock_runtime.invoke_model(
            modelId="anthropic.claude-3-sonnet-20240229-v1:0",
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body),
        )
        result = json.loads(response["body"].read())
        return result["content"][0]["text"].strip()
    except Exception as e:
        print("Bedrock invocation failed:", e)
        return f"Bedrock invocation failed: {e}"

# --------------------------
# RUN AWS CLI COMMAND
# --------------------------
def run_command_from_claude(prompt):
    print("Processing query:", prompt)
    command_prompt = (
        f'You are an expert in AWS CLI. Return a valid AWS CLI command for: "{prompt}". '
        f'Do not include explanations or placeholders. Default region: {session.get("aws_region", "us-east-1")}'
    )
    command = ask_bedrock(command_prompt)
    print("Command generated by Bedrock:", command)

    if not AWS_CLI_PATH:
        return command, "AWS CLI path not found in container."

    if command.strip().startswith("aws "):
        command = command.replace("aws", AWS_CLI_PATH, 1)

    aws_access = session.get("aws_access_key")
    aws_secret = session.get("aws_secret_key")
    aws_region = session.get("aws_region", "us-east-1")

    if not aws_access or not aws_secret:
        return command, "AWS credentials missing. Please login first."

    env = os.environ.copy()
    env["AWS_ACCESS_KEY_ID"] = aws_access
    env["AWS_SECRET_ACCESS_KEY"] = aws_secret
    env["AWS_DEFAULT_REGION"] = aws_region
    env["PATH"] = f"{os.path.dirname(AWS_CLI_PATH)}:" + env.get("PATH", "")

    try:
        output = subprocess.check_output(command, shell=True, stderr=subprocess.STDOUT, env=env)
        return command, output.decode()
    except subprocess.CalledProcessError as e:
        err = e.output.decode()
        print("Command execution failed:", err)
        if "InvalidClientTokenId" in err or "AuthFailure" in err:
            return command, "AWS session invalid. Please login again."
        return command, err

# --------------------------
# LOGIN ROUTES
# --------------------------
login_build_path = os.path.join(os.path.dirname(__file__), "login", "build")

@app.route("/login", defaults={"path": ""})
@app.route("/login/<path:path>")
def serve_login(path):
    full_path = os.path.join(login_build_path, path)
    if path != "" and os.path.exists(full_path):
        return send_from_directory(login_build_path, path)
    return send_from_directory(login_build_path, "index.html")

# --------------------------
# DEPLOYER FORWARDING (Dynamic service)
# --------------------------
@app.route("/deployer/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@app.route("/deployer/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def forward_to_deployer(path):
    target_url = urljoin(DEPLOYER_URL + "/", path)
    try:
        resp = requests.request(
            method=request.method,
            url=target_url,
            params=request.args if request.method=="GET" else None,
            data=request.data if request.method!="GET" else None,
            headers={key: value for key, value in request.headers if key.lower() != 'host'},
            timeout=10
        )
        excluded_headers = ["content-encoding", "content-length", "transfer-encoding", "connection"]
        headers = [(name, value) for (name, value) in resp.raw.headers.items() if name.lower() not in excluded_headers]
        return Response(resp.content, resp.status_code, headers)
    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 500

# --------------------------
# MAIN ROUTE
# --------------------------
@app.route("/")
def index():
    if not session.get("aws_access_key") or not session.get("aws_secret_key"):
        session.clear()
        return redirect("/login")
    return send_from_directory(app.static_folder, "index.html")

# --------------------------
# API ROUTES
# --------------------------
@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json()
    access_key = data.get("access_key")
    secret_key = data.get("secret_key")
    region = data.get("region")
    try:
        sts_client = boto3.client(
            "sts",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )
        identity = sts_client.get_caller_identity()
        session["aws_access_key"] = access_key
        session["aws_secret_key"] = secret_key
        session["aws_region"] = region
        session["aws_username"] = identity.get("Arn", "Unknown").split("/")[-1]
        session["aws_account_id"] = identity.get("Account", "")
        return jsonify({"success": True, "username": session["aws_username"]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"success": True})

@app.route("/api/user", methods=["GET"])
def api_user():
    if not session.get("aws_access_key") or not session.get("aws_secret_key"):
        session.clear()
        return jsonify({"logged_in": False, "error": "AWS session missing. Please login again."})
    return jsonify({
        "logged_in": True,
        "username": session.get("aws_username", ""),
        "region": session.get("aws_region", "")
    })

@app.route("/api/ask", methods=["POST"])
def api_handler():
    data = request.get_json()
    query = data.get("query")
    if not query:
        return jsonify({"error": "No query provided"}), 400

    user_id = session.get("aws_username", "unknown")
    action = query.lower()

    if any(word in action for word in ["create", "delete", "modify", "update"]):
        log_to_monitor(
            user_id=user_id,
            service="flask-app",
            endpoint="/api/ask",
            action_type="query",
            request_data={"query": query},
            response_summary="confirmation required"
        )
        return jsonify({"confirmation_needed": True, "query": query})

    command, output = run_command_from_claude(query)
    formatted_output = f"Command: {command}\n{output.strip()}"

    log_to_monitor(
        user_id=user_id,
        service="flask-app",
        endpoint="/api/ask",
        action_type="query",
        request_data={"query": query},
        response_summary=formatted_output
    )

    save_to_history(query, formatted_output)
    return jsonify({"confirmation_needed": False, "output": formatted_output})

@app.route("/api/confirm", methods=["POST"])
def api_confirm():
    data = request.get_json()
    query = data.get("query")
    decision = data.get("decision")
    user_id = session.get("aws_username", "unknown")

    if decision.lower() != "accept":
        log_to_monitor(
            user_id=user_id,
            service="flask-app",
            endpoint="/api/confirm",
            action_type="decision",
            request_data={"query": query, "decision": decision},
            response_summary="declined"
        )
        return jsonify({"output": "Action declined."})

    command, output = run_command_from_claude(query)
    formatted_output = f"Command: {command}\n{output.strip()}"

    log_to_monitor(
        user_id=user_id,
        service="flask-app",
        endpoint="/api/confirm",
        action_type="decision",
        request_data={"query": query, "decision": decision},
        response_summary=formatted_output
    )

    save_to_history(query, formatted_output)
    return jsonify({"output": formatted_output})

@app.route("/api/history", methods=["GET"])
def api_history():
    history = get_history()
    return jsonify(history)

@app.route("/api/deployer", methods=["POST"])
def api_deployer():
    data = request.get_json()
    payload = data.get("payload", {})
    try:
        response = requests.post(f"{DEPLOYER_URL}/deployer-api/deploy", json=payload, timeout=10)
        response.raise_for_status()
        return jsonify(response.json())
    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 500

# --------------------------
# MAIN
# --------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

