import argparse
import json
import os
import subprocess
import sys
import time
try:
    from assistant.ipc import HTTPIPCClient
except ModuleNotFoundError:
    from ipc import HTTPIPCClient

client = HTTPIPCClient()

def start_daemon():
    status, _ = client.get_health()
    if status == 200:
        print("Jarvis Daemon is already running.")
        return True
        
    print("Starting Jarvis Daemon in the background...")
    daemon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daemon.py")
    
    # Platform-specific background flags
    creationflags = 0
    if sys.platform == "win32":
        # DETACHED_PROCESS = 0x00000008, CREATE_NEW_PROCESS_GROUP = 0x00000200
        creationflags = 0x00000008 | 0x00000200
        
    try:
        subprocess.Popen(
            [sys.executable, daemon_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=(sys.platform != "win32"),
            creationflags=creationflags
        )
    except Exception as e:
        print(f"Failed to start daemon process: {e}")
        return False
        
    # Wait for startup
    for _ in range(10):
        time.sleep(0.3)
        status, _ = client.get_health()
        if status == 200:
            print("Jarvis Daemon successfully started.")
            return True
            
    print("Daemon failed to respond in time. Check daemon.log.")
    return False

def stop_daemon():
    status, _ = client.get_health()
    if status == 0:
        print("Jarvis Daemon is not running.")
        return True
        
    print("Stopping Jarvis Daemon...")
    status, body = client.shutdown()
    if status == 200:
        print("Daemon stop request accepted.")
        return True
    else:
        print(f"Failed to stop daemon: {body}")
        return False

def query_daemon(text):
    status, body = client.send_query(text)
    if status == 200:
        res = json.loads(body)
        print("Jarvis Daemon Response:")
        print(f"Text Response: {res.get('response', '')}")
        print("Interpretation Metadata:")
        print(json.dumps(res.get("interpretation", {}), indent=2))
    else:
        print(f"Error querying daemon: {body}")

def load_resource(name, timeout):
    status, body = client.load_resource(name, timeout)
    if status == 200:
        print(f"Successfully registered lazy resource '{name}' with timeout {timeout}s.")
    else:
        print(f"Error registering resource: {body}")

def main():
    parser = argparse.ArgumentParser(description="Jarvis Personal Assistant CLI")
    group = parser.add_mutually_exclusive_group(required=True)
    
    group.add_argument("--start", action="store_true", help="Start the Jarvis daemon in the background")
    group.add_argument("--stop", action="store_true", help="Stop the running Jarvis daemon")
    group.add_argument("--health", action="store_true", help="Check the daemon health status")
    group.add_argument("--query", "-q", type=str, help="Send a natural language query to the daemon")
    group.add_argument("--load-resource", type=str, help="Load a lazy resource by name (for testing)")
    
    parser.add_argument("--timeout", type=int, default=5, help="Timeout in seconds for loaded resource")
    
    args = parser.parse_args()
    
    if args.start:
        start_daemon()
    elif args.stop:
        stop_daemon()
    elif args.health:
        status, body = client.get_health()
        if status == 200:
            data = json.loads(body)
            print("Jarvis Daemon status: RUNNING")
            print(json.dumps(data, indent=2))
        else:
            print("Jarvis Daemon status: NOT RUNNING")
            print(f"Details: {body}")
    elif args.query:
        query_daemon(args.query)
    elif args.load_resource:
        load_resource(args.load_resource, args.timeout)

if __name__ == "__main__":
    main()
