import abc
import asyncio
import json
import logging
import urllib.request
import urllib.error
try:
    from assistant.config import HOST, PORT, IDLE_UNLOAD_TIMEOUT
except ModuleNotFoundError:
    from config import HOST, PORT, IDLE_UNLOAD_TIMEOUT

logger = logging.getLogger("jarvis.ipc")

class IPCHandler(abc.ABC):
    @abc.abstractmethod
    async def handle_health(self) -> dict:
        pass

    @abc.abstractmethod
    async def handle_query(self, query: str) -> dict:
        pass

    @abc.abstractmethod
    async def handle_load_resource(self, name: str, timeout: int) -> dict:
        pass

    @abc.abstractmethod
    async def handle_shutdown(self) -> dict:
        pass

class IPCServer(abc.ABC):
    @abc.abstractmethod
    async def start(self):
        pass

    @abc.abstractmethod
    async def stop(self):
        pass

class IPCClient(abc.ABC):
    @abc.abstractmethod
    def get_health(self) -> tuple[int, str]:
        pass

    @abc.abstractmethod
    def send_query(self, query: str) -> tuple[int, str]:
        pass

    @abc.abstractmethod
    def load_resource(self, name: str, timeout: int) -> tuple[int, str]:
        pass

    @abc.abstractmethod
    def shutdown(self) -> tuple[int, str]:
        pass

class HTTPIPCServer(IPCServer):
    def __init__(self, handler: IPCHandler, host=HOST, port=PORT):
        self.handler = handler
        self.host = host
        self.port = port
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self.handle_client, self.host, self.port)
        logger.info(f"HTTPIPCServer listening on http://{self.host}:{self.port}")
        try:
            async with self.server:
                await self.server.serve_forever()
        except asyncio.CancelledError:
            pass

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            logger.info("HTTPIPCServer stopped.")

    async def handle_client(self, reader, writer):
        try:
            header_data = b""
            while b"\r\n\r\n" not in header_data:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                header_data += chunk
                if len(header_data) > 65536:
                    break
            
            if not header_data:
                writer.close()
                await writer.wait_closed()
                return
                
            parts = header_data.split(b"\r\n\r\n", 1)
            headers_part = parts[0]
            extra_body = parts[1] if len(parts) > 1 else b""
            
            lines = headers_part.decode("utf-8", errors="ignore").split("\r\n")
            req_line = lines[0]
            req_parts = req_line.split()
            if len(req_parts) < 2:
                writer.close()
                await writer.wait_closed()
                return
                
            method, path = req_parts[0], req_parts[1]
            
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
                    
            content_length = int(headers.get("content-length", 0))
            body = extra_body
            if len(body) < content_length:
                remaining = content_length - len(body)
                body += await reader.readexactly(remaining)
                
            status_code, resp_bytes, content_type = await self.process_request(method, path, body)
            
            resp = (
                f"HTTP/1.1 {status_code} OK\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(resp_bytes)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("utf-8") + resp_bytes
            
            writer.write(resp)
            await writer.drain()
        except Exception as e:
            logger.error(f"Error handling IPC connection: {e}")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def process_request(self, method, path, body_bytes):
        path = path.split("?")[0]
        
        if method == "GET" and path == "/health":
            resp_dict = await self.handler.handle_health()
            return 200, json.dumps(resp_dict).encode("utf-8"), "application/json"
            
        elif method == "POST" and path == "/query":
            try:
                data = json.loads(body_bytes.decode("utf-8"))
                query = data.get("query", "")
                resp_dict = await self.handler.handle_query(query)
                return 200, json.dumps(resp_dict).encode("utf-8"), "application/json"
            except Exception as e:
                return 400, json.dumps({"error": f"Invalid JSON payload: {e}"}).encode("utf-8"), "application/json"
                
        elif method == "POST" and path == "/shutdown":
            resp_dict = await self.handler.handle_shutdown()
            return 200, json.dumps(resp_dict).encode("utf-8"), "application/json"
            
        elif method == "POST" and path == "/resource/load":
            try:
                data = json.loads(body_bytes.decode("utf-8"))
                name = data.get("name", "")
                timeout = data.get("timeout", IDLE_UNLOAD_TIMEOUT)
                if not name:
                    return 400, b"Missing resource name", "text/plain"
                resp_dict = await self.handler.handle_load_resource(name, timeout)
                return 200, json.dumps(resp_dict).encode("utf-8"), "application/json"
            except Exception as e:
                return 400, str(e).encode("utf-8"), "text/plain"
                
        else:
            return 404, b"Not Found", "text/plain"

class HTTPIPCClient(IPCClient):
    def __init__(self, host=HOST, port=PORT):
        self.base_url = f"http://{host}:{port}"

    def get_health(self) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=2) as response:
                return response.status, response.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            return 0, str(e)

    def send_post(self, path: str, data: dict = None, timeout: int = 35) -> tuple[int, str]:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, method="POST")
        req.add_header("Content-Type", "application/json")
        json_data = json.dumps(data or {}).encode("utf-8")
        req.add_header("Content-Length", str(len(json_data)))
        
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, data=json_data, timeout=timeout) as response:
                    return response.status, response.read().decode("utf-8")
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                if attempt < 2 and any(kw in str(e).lower() for kw in ("refused", "reset", "failed to connect")):
                    time.sleep(0.4)
                    continue
                return 0, str(e)

    def send_query(self, query: str, timeout: int = 35) -> tuple[int, str]:
        return self.send_post("/query", {"query": query}, timeout=timeout)

    def load_resource(self, name: str, timeout: int) -> tuple[int, str]:
        return self.send_post("/resource/load", {"name": name, "timeout": timeout})

    def shutdown(self) -> tuple[int, str]:
        return self.send_post("/shutdown")
