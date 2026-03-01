import subprocess
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import yaml
import os
import concurrent.futures
from flask import Flask, request, jsonify, send_file, render_template
import tempfile
from github import Github
import posixpath
import socket
import ipaddress
import shutil
import dns.resolver
import dns.exception
import re
import time
import uuid
import http.client
import base64
import urllib3

# 禁用 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def resolve_doh(domain, doh_url, dns_servers=None, record_types=None, max_depth=8):
    if record_types is None:
        record_types = ['A', 'AAAA']

    session = requests.Session()

    def _query_single(qname, record_type, visited, depth=0):
        if depth >= max_depth or qname in visited:
            return set()
        visited.add(qname)

        ips = set()
        cname_targets = []

        try:
            response = session.get(
                doh_url,
                params={'name': qname, 'type': record_type},
                headers={'Accept': 'application/dns-json'},
                timeout=(DNS_RESOLVER_TIMEOUT, DNS_RESOLVER_LIFETIME),
                verify=False
            )
            if response.status_code == 200:
                data = response.json()
                for answer in data.get('Answer', []):
                    rtype = answer.get('type')
                    rdata = answer.get('data', '').rstrip('.')
                    if rtype in (1, 28):  # A or AAAA
                        if not is_private_ip(rdata):
                            ips.add(rdata)
                    elif rtype == 5:  # CNAME
                        if rdata and rdata not in visited:
                            cname_targets.append(rdata)
        except Exception as e:
            print(f"DoH query failed for {qname} ({record_type}): {e}")
            return ips
        
        if not ips:
            for target in cname_targets:
                ips.update(_query_single(target, record_type, visited, depth + 1))

        return ips

    results = set()
    for record_type in record_types:
        results.update(_query_single(domain, record_type, set()))

    return list(results)


def is_doh_server(server):
    return isinstance(server, str) and server.startswith(('https://', 'http://'))


def filter_doh_servers(servers):
    doh_servers = []
    udp_servers = []
    for server in servers:
        if is_doh_server(server):
            doh_servers.append(server)
        else:
            udp_servers.append(server)
    return doh_servers, udp_servers

APP_TEMP_DIR = os.path.join(tempfile.gettempdir(), "yamlforge_temp")
if not os.path.exists(APP_TEMP_DIR):
    os.makedirs(APP_TEMP_DIR, exist_ok=True)

FILE_CLEANUP_TIMEOUT = 3600

DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", 600))
DNS_RESOLVER_TIMEOUT = int(os.environ.get("DNS_RESOLVER_TIMEOUT", 5))
DNS_RESOLVER_LIFETIME = int(os.environ.get("DNS_RESOLVER_LIFETIME", 10))
RETRY_BACKOFF_FACTOR = float(os.environ.get("RETRY_BACKOFF_FACTOR", 1))
RETRY_TOTAL = int(os.environ.get("RETRY_TOTAL", 5))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", 10))
DOWNLOAD_ATTEMPTS = int(os.environ.get("DOWNLOAD_ATTEMPTS", 5))
DOWNLOAD_RETRY_WAIT = float(os.environ.get("DOWNLOAD_RETRY_WAIT", 2))

def cleanup_stale_files():
    now = time.time()
    try:
        for filename in os.listdir(APP_TEMP_DIR):
            file_path = os.path.join(APP_TEMP_DIR, filename)
            try:
                if os.path.isfile(file_path):
                    file_age = now - os.path.getmtime(file_path)
                    if file_age > FILE_CLEANUP_TIMEOUT:
                        os.remove(file_path)
            except OSError:
                pass
    except Exception as e:
        print(f"Error during file cleanup: {e}")

def download_file(url, destination_path=None, proxies=None):
    cleanup_stale_files()

    if destination_path is None:
        destination_path = os.path.join(APP_TEMP_DIR, f"{uuid.uuid4()}.tmp")

    session = requests.Session()
    retries = Retry(total=RETRY_TOTAL, backoff_factor=RETRY_BACKOFF_FACTOR, status_forcelist=[500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.mount("http://", HTTPAdapter(max_retries=retries))

    last_exception = None
    for attempt in range(DOWNLOAD_ATTEMPTS):
        try:
            with session.get(url, stream=True, proxies=proxies, timeout=DOWNLOAD_TIMEOUT) as response:
                response.raise_for_status()
                expected_length_header = response.headers.get("content-length")
                try:
                    expected_length = int(expected_length_header) if expected_length_header else None
                except ValueError:
                    expected_length = None
                downloaded_bytes = 0
                with open(destination_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded_bytes += len(chunk)
                if expected_length is not None and downloaded_bytes < expected_length:
                    raise requests.exceptions.ContentDecodingError(
                        f"Incomplete download: expected {expected_length} bytes, got {downloaded_bytes}"
                    )
            return destination_path
        except (
            requests.exceptions.RequestException,
            requests.exceptions.ChunkedEncodingError,
            http.client.IncompleteRead,
        ) as e:
            last_exception = e
            print(f"Download attempt {attempt + 1} failed for {url}: {e}. Retrying...")
            if os.path.exists(destination_path):
                try:
                    os.remove(destination_path)
                except OSError:
                    pass
            time.sleep(DOWNLOAD_RETRY_WAIT * (attempt + 1))

    if isinstance(last_exception, http.client.IncompleteRead):
        raise requests.exceptions.ConnectionError(
            f"Incomplete download after {DOWNLOAD_ATTEMPTS} attempts: {last_exception}"
        ) from last_exception
    raise last_exception or Exception("Unknown download error")


app = Flask(__name__, static_folder="assets", static_url_path="/assets")

env = os.environ.copy()
if not env.get("NODE_PATH"):
    try:
        env["NODE_PATH"] = subprocess.check_output(["npm", "root", "-g"], shell=True).decode().strip()
    except Exception:
        env["NODE_PATH"] = ""
API_KEYS = [k.strip() for k in os.environ.get("API_KEY", "").split(",") if k.strip()]


def extract_servers(data, field=None, max_depth=8):
    servers = set()
    domain_pattern = re.compile(
        r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,6}"
    )
    ipv4_pattern = re.compile(
        r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b"
    )
    ipv6_pattern = re.compile(
        r"(([0-9a-fA-F]{1,4}:){7,7}[0-9a-fA-F]{1,4}|([0-9a-fA-F]{1,4}:){1,7}:|([0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}|([0-9a-fA-F]{1,4}:){1,5}(:[0-9a-fA-F]{1,4}){1,2}|([0-9a-fA-F]{1,4}:){1,4}(:[0-9a-fA-F]{1,4}){1,3}|([0-9a-fA-F]{1,4}:){1,3}(:[0-9a-fA-F]{1,4}){1,4}|([0-9a-fA-F]{1,4}:){1,2}(:[0-9a-fA-F]{1,4}){1,5}|[0-9a-fA-F]{1,4}:((:[0-9a-fA-F]{1,4}){1,6})|:((:[0-9a-fA-F]{1,4}){1,7}|:)|fe80:(:[0-9a-fA-F]{0,4}){0,4}%[0-9a-zA-Z]{1,}|::(ffff(:0{1,4}){0,1}:){0,1}((25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9])\.){3,3}(25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9])|([0-9a-fA-F]{1,4}:){1,4}:((25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9])\.){3,3}(25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9]))"
    )

    def extract_from_dict(data, depth=0):
        if depth > max_depth:
            return
        if not isinstance(data, dict):
            return
        for key, value in data.items():
            if isinstance(value, str):
                if ipv4_pattern.match(value) or ipv6_pattern.match(value):
                    servers.add(value)
                elif domain_pattern.match(value):
                    servers.add(value)
            elif isinstance(value, dict):
                extract_from_dict(value, depth + 1)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        extract_from_dict(item, depth + 1)
                    elif isinstance(item, str):
                        if ipv4_pattern.match(item) or ipv6_pattern.match(item):
                            servers.add(item)
                        elif domain_pattern.match(item):
                            servers.add(item)

    if field:
        field_data = extract_field(data, field, max_depth=max_depth)
        if isinstance(field_data, dict):
            extract_from_dict(field_data)
        elif isinstance(field_data, list):
            for item in field_data:
                if isinstance(item, dict):
                    extract_from_dict(item)
                elif isinstance(item, str):
                    if ipv4_pattern.match(item) or ipv6_pattern.match(item):
                        servers.add(item)
                    elif domain_pattern.match(item):
                        servers.add(item)
    else:
        extract_from_dict(data)
    return list(servers)


def extract_field(data, field, max_depth=8, current_depth=0):
    if current_depth > max_depth:
        return None

    keys = field.split(".")
    for key in keys:
        if isinstance(data, list):
            data = [
                extract_field(item, key, max_depth, current_depth + 1) for item in data
            ]
        elif isinstance(data, dict):
            data = data.get(key)
        else:
            return None
        if data is None:
            return None
    return data


def extract_server_port_map(data, max_depth=8):
    proxies = extract_field(data, "proxies", max_depth=max_depth)
    server_port_map = {}

    if isinstance(proxies, list):
        for item in proxies:
            if isinstance(item, dict):
                server = item.get("server")
                port = item.get("port")
                if server and port is not None:
                    server_key = str(server)
                    if server_key not in server_port_map:
                        server_port_map[server_key] = []
                    port_str = str(port)
                    if port_str not in server_port_map[server_key]:
                        server_port_map[server_key].append(port_str)

    return server_port_map


def format_host_with_port(host, port):
    if not port:
        return host

    stripped_host = host.strip("[]")

    try:
        ip_obj = ipaddress.ip_address(stripped_host)
        if ip_obj.version == 6:
            return f"[{stripped_host}]:{port}"
    except ValueError:
        pass

    return f"{host}:{port}"

PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
]


def is_private_ip(ip_address):
    try:
        addr = ipaddress.ip_address(ip_address)
        return any(addr in network for network in PRIVATE_NETWORKS)
    except ipaddress.AddressValueError:
        return False


def resolve_domain_recursive(domain, dns_servers, max_depth=8):
    unique_servers = set()
    results = []

    # 分离 DoH 服务器和 UDP DNS 服务器
    doh_servers, udp_servers = filter_doh_servers(dns_servers)

    def resolve_single(domain, record_type, udp_servers, doh_servers, depth):
        if depth >= max_depth:
            return []

        resolved_items = []

        if domain not in unique_servers:
            unique_servers.add(domain)
            resolved_items.append(f"DOMAIN:{domain}")
        
        if udp_servers:
            resolver = dns.resolver.Resolver()
            resolver.nameservers = udp_servers
            resolver.lifetime = DNS_RESOLVER_LIFETIME
            resolver.timeout = DNS_RESOLVER_TIMEOUT

            try:
                answers = resolver.resolve(domain, record_type)
                for rdata in answers:
                    ip_or_cname = rdata.to_text().strip(".")
                    if ip_or_cname not in unique_servers:
                        unique_servers.add(ip_or_cname)
                        if record_type == "CNAME":
                            resolved_items.append(f"DOMAIN:{ip_or_cname}")
                            resolved_items.extend(
                                resolve_single(ip_or_cname, "A", udp_servers, doh_servers, depth + 1)
                            )
                            resolved_items.extend(
                                resolve_single(ip_or_cname, "AAAA", udp_servers, doh_servers, depth + 1)
                            )

                        elif not is_private_ip(ip_or_cname):
                            resolved_items.append(ip_or_cname)
            except (
                dns.resolver.NoAnswer,
                dns.resolver.NXDOMAIN,
                dns.resolver.NoNameservers,
                dns.exception.Timeout,
                dns.resolver.LifetimeTimeout,
            ):
                pass
            except Exception as e:
                print(f"Unexpected error resolving {domain} ({record_type}): {e}")
                pass

        for doh_url in doh_servers:
            try:
                ips = resolve_doh(domain, doh_url, dns_servers, max_depth=max_depth - depth)
                for ip in ips:
                    if ip not in unique_servers:
                        unique_servers.add(ip)
                        if not is_private_ip(ip):
                            resolved_items.append(ip)
            except Exception as e:
                print(f"DoH resolution failed for {domain} via {doh_url}: {e}")
                continue


        return resolved_items

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(resolve_single, domain, record_type, udp_servers, doh_servers, 0)
            for record_type in ["A", "AAAA", "CNAME"]
        ]
        for future in concurrent.futures.as_completed(futures):
            try:
                results.extend(future.result())
            except Exception as e:
                print(f"Error in resolving future: {e}")

    return results


def generate_server_list(servers, dns_servers, max_depth=8, server_port_map=None):
    unique_servers = set()
    all_results = []

    def format_output(value, server=None):
        base_value = value[7:] if value.startswith("DOMAIN:") else value

        if server and server_port_map:
            ports = server_port_map.get(str(server))
            if ports:
                return [format_host_with_port(base_value, port) for port in ports]

        return [base_value]

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_server = {
            executor.submit(
                resolve_domain_recursive, server, dns_servers, max_depth
            ): server
            for server in servers
            if ":" not in server
            and (not "." in server or not server.replace(".", "").isdigit())
        }

        for future in concurrent.futures.as_completed(future_to_server):
            server = future_to_server[future]
            try:
                results = future.result()
                for item in results:
                    formatted_items = format_output(item, server)
                    for formatted_item in formatted_items:
                        if formatted_item not in unique_servers:
                            unique_servers.add(formatted_item)
                            all_results.append(formatted_item)

            except Exception as e:
                print(f"Error resolving {server}: {e}")

    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
        filename = f.name
        for result in all_results:
            f.write(f"{result}\n")

        for server in servers:
            formatted_servers = format_output(server, server)
            base_value = server[7:] if server.startswith("DOMAIN:") else server

            for formatted_server in formatted_servers:
                if formatted_server not in unique_servers:
                    if ":" in server:
                        if not is_private_ip(base_value):
                            f.write(f"{formatted_server}\n")
                            unique_servers.add(formatted_server)
                    elif server.replace(".", "").isdigit():
                        if not is_private_ip(base_value):
                            f.write(f"{formatted_server}\n")
                            unique_servers.add(formatted_server)
    return filename


def upload_to_github(
    filename, repo_name, token, branch="main", path="", rename="yaml.list"
):

    g = Github(token)

    repo = g.get_repo(repo_name)
    file_path = posixpath.join(path, rename)

    contents = None
    file_exists = False
    try:
        contents = repo.get_contents(file_path, ref=branch)
        if isinstance(contents, list):
            raise ValueError(
                f"Path '{file_path}' refers to a directory, please provide a file path"
            )
        file_exists = True
    except Exception as e:
        if isinstance(e, ValueError):
            raise e
        if "Not Found" in str(e):
            file_exists = False
        else:
            raise e

    with open(filename, "r") as f:
        file_content = f.read()

    if file_exists:
        if contents is None:
            raise RuntimeError("Expected existing file contents but none were retrieved")
        try:
            repo.update_file(
                contents.path,
                "Update proxies.server list",
                file_content,
                contents.sha,
                branch=branch,
            )
        except Exception as e:
            raise e
    else:
        try:
            repo.create_file(
                file_path,
                "Add proxies.server list",
                file_content,
                branch=branch,
            )
        except Exception as e:
            raise e


def process_yaml_with_js(yaml_file_path, js_file_path):
    with open(js_file_path, "r", encoding="utf-8") as js_file:
        js_code = js_file.read()

    with tempfile.NamedTemporaryFile(
        "w", delete=False, suffix=".yaml", encoding="utf-8"
    ) as temp_processed_yaml:
        temp_processed_yaml_path = temp_processed_yaml.name

    safe_yaml_path = yaml_file_path.replace('\\', '/')
    safe_temp_path = temp_processed_yaml_path.replace('\\', '/')

    node_script = f"""
    const fs = require('fs');
    const yaml = require('js-yaml');
    const iconv = require('iconv-lite');

    {js_code}

    function processYaml() {{
        const yamlInput = fs.readFileSync('{safe_yaml_path}');
        let yamlStr = iconv.decode(yamlInput, 'utf-8');
        
        if (yamlStr.charCodeAt(0) === 0xFEFF) {{
            yamlStr = yamlStr.slice(1);
        }}

        const config = yaml.load(yamlStr);
        const modifiedConfig = main(config);
        const output = yaml.dump(modifiedConfig, {{ encoding: 'utf-8' }});
        fs.writeFileSync('{safe_temp_path}', output, 'utf-8');
    }}

    processYaml();
    """

    with tempfile.NamedTemporaryFile(
        "w", delete=False, suffix=".js", encoding="utf-8"
    ) as temp_node_file:
        temp_node_file.write(node_script)
        temp_node_file_path = temp_node_file.name

    try:
        subprocess.run(["node", temp_node_file_path], env=env, check=True)
        shutil.move(temp_processed_yaml_path, yaml_file_path)
    except subprocess.CalledProcessError as e:
        print(f"Error running node script: {e}")
        raise e
    finally:
        os.remove(temp_node_file_path)
        if os.path.exists(temp_processed_yaml_path):
            os.remove(temp_processed_yaml_path)


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/listget", methods=["GET"])
def listget():
    provided_api_key = request.args.get("api_key", "")
    source = request.args.get("source")
    proxy = request.args.get("proxy", "")
    field = request.args.get("field", "proxies.server")
    repo = request.args.get("repo")
    token = request.args.get("token")
    branch = request.args.get("branch", "main")
    path = request.args.get("path", "")
    filename = request.args.get("filename", "yaml.list")
    dns_servers_str = request.args.get("dns_servers")
    max_depth_str = request.args.get("max_depth", 8)
    resolve_domains = request.args.get("resolve_domains", "false").lower() == "true"

    if API_KEYS:
        if provided_api_key not in API_KEYS:
            return jsonify({"error": "Invalid API key"}), 403

    try:
        max_depth = int(max_depth_str)
    except ValueError:
        max_depth = 8

    if not source:
        return jsonify({"error": "Missing source parameter"}), 400

    try:
        proxies = {}
        if proxy:
            if proxy.startswith("socks"):
                proxies = {"http": proxy, "https": proxy}
            elif proxy.startswith("http"):
                proxies = {"http": proxy, "https": proxy}
            else:
                return jsonify({"error": "Invalid proxy format"}), 400

        temp_source_path = None
        try:
            temp_source_path = download_file(source, proxies=proxies)
            with open(temp_source_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except requests.exceptions.Timeout:
            return jsonify({"error": "Request timed out while fetching source"}), 408
        except requests.exceptions.SSLError:
            return jsonify({"error": "SSL verification failed"}), 495
        except requests.exceptions.RequestException as e:
            print(f"Network error while fetching source {source}: {e}")
            return (
                jsonify(
                    {
                        "error": "Network error while fetching source, please retry or provide a mirror URL",
                        "detail": str(e),
                    }
                ),
                502,
            )
        except yaml.YAMLError as e:
            return jsonify({"error": f"Invalid YAML format: {str(e)}"}), 400
        except Exception as e:
            return jsonify({"error": f"Unexpected error: {str(e)}"}), 500
        finally:
            if temp_source_path and os.path.exists(temp_source_path):
                try:
                    os.remove(temp_source_path)
                except OSError:
                    pass

    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500


    port_requested = field.endswith(".port")
    effective_field = field[: -len(".port")] if port_requested else field
    server_port_map = extract_server_port_map(data, max_depth=max_depth) if port_requested else {}

    if resolve_domains:
        servers = extract_servers(data, effective_field, max_depth=max_depth)
    else:
        servers = extract_field(data, effective_field, max_depth=max_depth)
        if not servers:
            return jsonify({"error": f"Field '{effective_field}' not found in YAML"}), 400
        if isinstance(servers, list):
            servers = [s for s in servers if s]
        else:
            return jsonify({"error": "Invalid field structure"}), 400

    if port_requested:
        if not server_port_map:
            return jsonify({"error": "Port data not found for proxies.server.port"}), 400
        missing_ports = [s for s in servers if not server_port_map.get(str(s))]
        if missing_ports:
            return (
                jsonify({"error": f"Port not found for servers: {', '.join(map(str, missing_ports))}"}),
                400,
            )

    if resolve_domains:
        if dns_servers_str:
            dns_servers = dns_servers_str.split(",")
        else:
            dns_servers = ["223.5.5.5", "8.8.8.8"]

        for dns_server in dns_servers:
            if is_doh_server(dns_server):
                continue
            socket.getaddrinfo(dns_server, None, socket.AF_UNSPEC, socket.SOCK_STREAM)

        temp_filename = generate_server_list(
            servers,
            dns_servers,
            max_depth=max_depth,
            server_port_map=server_port_map if port_requested else None,
        )
    else:
        temp_filename = tempfile.mktemp(suffix=".txt")
        with open(temp_filename, "w", encoding="utf-8") as f:
            for server in servers:
                if port_requested:
                    ports = server_port_map.get(str(server), [])
                    for port in ports:
                        f.write(f"{format_host_with_port(server, port)}\n")
                else:
                    f.write(f"{server}\n")

    try:
        if repo and token:
            try:
                upload_to_github(temp_filename, repo, token, branch, path, filename)
                return jsonify(
                    {
                        "message": f"File uploaded to GitHub successfully at {os.path.join(path, filename)}"
                    }
                )
            except Exception as e:
                return (
                    jsonify({"error": f"Failed to upload to GitHub: {str(e)}"}),
                    500,
                )
        else:
            return send_file(temp_filename, as_attachment=True, download_name=filename)
    finally:
        os.remove(temp_filename)


@app.route("/yamlprocess", methods=["GET"])
def yamlprocess():
    provided_api_key = request.args.get("api_key", "")
    source_url = request.args.get("source")
    proxy = request.args.get("proxy", "")
    merge_url = request.args.get("merge")
    filename = request.args.get("filename")

    if API_KEYS:
        if provided_api_key not in API_KEYS:
            return jsonify({"error": "Invalid API key"}), 403

    if not source_url or not merge_url:
        return jsonify({"error": "Missing source or merge URL"}), 400

    try:
        proxies = {}
        if proxy:
            if proxy.startswith("socks"):
                proxies = {"http": proxy, "https": proxy}
            elif proxy.startswith("http"):
                proxies = {"http": proxy, "https": proxy}
            else:
                return jsonify({"error": "Invalid proxy format"}), 400

        temp_yaml_file_path = None
        temp_js_file_path = None
        try:
            temp_yaml_file_path = download_file(source_url, proxies=proxies)
            temp_js_file_path = download_file(merge_url, proxies=proxies)

            process_yaml_with_js(temp_yaml_file_path, temp_js_file_path)

            download_filename = filename or os.path.basename(source_url)
            return send_file(
                temp_yaml_file_path, as_attachment=True, download_name=download_filename
            )
        finally:
            if temp_js_file_path and os.path.exists(temp_js_file_path):
                try:
                    os.remove(temp_js_file_path)
                except OSError:
                    pass
            if temp_yaml_file_path and os.path.exists(temp_yaml_file_path):
                try:
                    os.remove(temp_yaml_file_path)
                except OSError:
                    pass

    except requests.exceptions.Timeout:
        return jsonify({"error": "Request timed out while fetching files"}), 408
    except requests.exceptions.SSLError:
        return jsonify({"error": "SSL verification failed"}), 495
    except requests.exceptions.RequestException as e:
        print(f"Network error while fetching files: {e}")
        return (
            jsonify(
                {
                    "error": "Network error while fetching files, please retry or provide a mirror URL",
                    "detail": str(e),
                }
            ),
            502,
        )
    except Exception as e:
        return jsonify({"error": f"Error processing files: {e}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=19527)
