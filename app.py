import concurrent.futures
import http.client
import ipaddress
import logging
import os
import posixpath
import re
import shutil
import socket
import subprocess
import tempfile
import time
import uuid

import dns.exception
import dns.resolver
import requests
import urllib3
import yaml
from flask import Flask, after_this_request, jsonify, render_template, request, send_file
from github import Github
from github.ContentFile import ContentFile
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def resolve_doh(domain, doh_url, record_types=None, max_depth=8):
    if record_types is None:
        record_types = ["A", "AAAA"]

    with requests.Session() as session:
        def _query_single(qname, record_type, visited, depth=0):
            if depth >= max_depth or qname in visited:
                return set()
            visited.add(qname)

            ips = set()
            cname_targets = []

            try:
                response = session.get(
                    doh_url,
                    params={"name": qname, "type": record_type},
                    headers={"Accept": "application/dns-json"},
                    timeout=(DNS_RESOLVER_TIMEOUT, DNS_RESOLVER_LIFETIME),
                    verify=False,
                )
                if response.status_code == 200:
                    data = response.json()
                    for answer in data.get("Answer", []):
                        rtype = answer.get("type")
                        rdata = answer.get("data", "").rstrip(".")
                        if rtype in (1, 28):
                            if not is_private_ip(rdata):
                                ips.add(rdata)
                        elif rtype == 5 and rdata and rdata not in visited:
                            cname_targets.append(rdata)
            except Exception as exc:
                logger.warning(
                    "DoH query failed for %s (%s) via %s: %s",
                    qname,
                    record_type,
                    doh_url,
                    exc,
                )
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
    return isinstance(server, str) and server.startswith(("https://", "http://"))


def filter_doh_servers(servers):
    doh_servers = []
    udp_servers = []
    for server in servers:
        if is_doh_server(server):
            doh_servers.append(server)
        else:
            udp_servers.append(parse_udp_dns_server(server))
    return doh_servers, udp_servers


DEFAULT_DNS_SERVERS = ["223.5.5.5", "8.8.8.8"]


def normalize_dns_server_entries(values):
    normalized_servers = []
    seen_servers = set()

    for value in values or []:
        if value is None:
            continue

        for entry in str(value).split(","):
            server = entry.strip()
            if server and server not in seen_servers:
                seen_servers.add(server)
                normalized_servers.append(server)

    return normalized_servers


def parse_dns_server_port(port_str, server):
    try:
        port = int(str(port_str).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid DNS server '{server}': port must be an integer between 1 and 65535."
        ) from exc

    if not 1 <= port <= 65535:
        raise ValueError(
            f"Invalid DNS server '{server}': port must be between 1 and 65535."
        )

    return port


def parse_udp_dns_server(server):
    if not isinstance(server, str):
        raise ValueError("DNS server must be a string.")

    candidate = server.strip()
    if not candidate:
        raise ValueError("DNS server cannot be empty.")

    if is_doh_server(candidate):
        raise ValueError(f"DoH server '{candidate}' is not a UDP DNS server.")

    if candidate.startswith("["):
        bracket_end = candidate.find("]")
        if bracket_end == -1:
            raise ValueError(
                f"Invalid DNS server '{candidate}': missing closing ']' for IPv6 address."
            )

        host = candidate[1:bracket_end].strip()
        remainder = candidate[bracket_end + 1:].strip()
        if not host:
            raise ValueError(f"Invalid DNS server '{candidate}': host cannot be empty.")

        if not remainder:
            return {"host": host, "port": 53}

        if not remainder.startswith(":"):
            raise ValueError(
                f"Invalid DNS server '{candidate}': unexpected characters after ']'."
            )

        return {"host": host, "port": parse_dns_server_port(remainder[1:], candidate)}

    try:
        return {"host": str(ipaddress.ip_address(candidate)), "port": 53}
    except ValueError:
        pass

    host = candidate
    port = 53

    if ":" in candidate:
        host_part, port_part = candidate.rsplit(":", 1)
        host = host_part.strip()
        if ":" in host:
            raise ValueError(
                f"Invalid DNS server '{candidate}': use [IPv6]:port when specifying a port for an IPv6 address."
            )
        if not host:
            raise ValueError(f"Invalid DNS server '{candidate}': host cannot be empty.")
        port = parse_dns_server_port(port_part, candidate)

    return {"host": host, "port": port}

APP_TEMP_DIR = os.path.join(tempfile.gettempdir(), "yamlforge_temp")
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


def remove_file(path):
    if not path or not os.path.exists(path):
        return
    try:
        os.remove(path)
    except OSError:
        pass


def cleanup_stale_files():
    now = time.time()
    try:
        for filename in os.listdir(APP_TEMP_DIR):
            file_path = os.path.join(APP_TEMP_DIR, filename)
            try:
                if os.path.isfile(file_path):
                    file_age = now - os.path.getmtime(file_path)
                    if file_age > FILE_CLEANUP_TIMEOUT:
                        remove_file(file_path)
            except OSError:
                pass
    except OSError as exc:
        logger.warning("Failed to clean temporary files in %s: %s", APP_TEMP_DIR, exc)


def download_file(url, destination_path=None, proxies=None):
    cleanup_stale_files()

    if destination_path is None:
        destination_path = os.path.join(APP_TEMP_DIR, f"{uuid.uuid4()}.tmp")

    retries = Retry(
        total=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF_FACTOR,
        status_forcelist=[500, 502, 503, 504],
    )
    last_exception = None

    with requests.Session() as session:
        session.mount("https://", HTTPAdapter(max_retries=retries))
        session.mount("http://", HTTPAdapter(max_retries=retries))

        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                with session.get(
                    url,
                    stream=True,
                    proxies=proxies,
                    timeout=DOWNLOAD_TIMEOUT,
                ) as response:
                    response.raise_for_status()
                    expected_length_header = response.headers.get("content-length")
                    try:
                        expected_length = (
                            int(expected_length_header)
                            if expected_length_header
                            else None
                        )
                    except ValueError:
                        expected_length = None

                    downloaded_bytes = 0
                    with open(destination_path, "wb") as file:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                file.write(chunk)
                                downloaded_bytes += len(chunk)

                    if (
                        expected_length is not None
                        and downloaded_bytes < expected_length
                    ):
                        raise requests.exceptions.ContentDecodingError(
                            f"Incomplete download: expected {expected_length} bytes, got {downloaded_bytes}"
                        )

                return destination_path
            except (
                requests.exceptions.RequestException,
                requests.exceptions.ChunkedEncodingError,
                http.client.IncompleteRead,
            ) as exc:
                last_exception = exc
                logger.warning(
                    "Download attempt %s/%s failed for %s: %s",
                    attempt,
                    DOWNLOAD_ATTEMPTS,
                    url,
                    exc,
                )
                remove_file(destination_path)
                time.sleep(DOWNLOAD_RETRY_WAIT * attempt)

    if isinstance(last_exception, http.client.IncompleteRead):
        raise requests.exceptions.ConnectionError(
            f"Incomplete download after {DOWNLOAD_ATTEMPTS} attempts: {last_exception}"
        ) from last_exception
    raise last_exception or Exception("Unknown download error")


app = Flask(__name__, static_folder="assets", static_url_path="/assets")

env = os.environ.copy()
if not env.get("NODE_PATH"):
    try:
        env["NODE_PATH"] = subprocess.check_output(
            ["npm", "root", "-g"], shell=True
        ).decode().strip()
    except Exception:
        env["NODE_PATH"] = ""
API_KEYS = [k.strip() for k in os.environ.get("API_KEY", "").split(",") if k.strip()]


def build_proxy_config(proxy):
    if not proxy:
        return {}
    if proxy.startswith(("socks", "http")):
        return {"http": proxy, "https": proxy}
    raise ValueError("Invalid proxy format")


def parse_max_depth(value, default=8):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def is_authorized_request(provided_api_key):
    return not API_KEYS or provided_api_key in API_KEYS


def send_download(path, download_name):
    @after_this_request
    def cleanup(response):
        remove_file(path)
        return response

    return send_file(path, as_attachment=True, download_name=download_name)


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

    def add_server(value):
        if (
            ipv4_pattern.match(value)
            or ipv6_pattern.match(value)
            or domain_pattern.match(value)
        ):
            servers.add(value)

    def extract_from_dict(data, depth=0):
        if depth > max_depth:
            return
        if not isinstance(data, dict):
            return
        for value in data.values():
            if isinstance(value, str):
                add_server(value)
            elif isinstance(value, dict):
                extract_from_dict(value, depth + 1)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        extract_from_dict(item, depth + 1)
                    elif isinstance(item, str):
                        add_server(item)

    if field:
        field_data = extract_field(data, field, max_depth=max_depth)
        if isinstance(field_data, dict):
            extract_from_dict(field_data)
        elif isinstance(field_data, list):
            for item in field_data:
                if isinstance(item, dict):
                    extract_from_dict(item)
                elif isinstance(item, str):
                    add_server(item)
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
    doh_servers, udp_servers = filter_doh_servers(dns_servers)

    def resolve_single(name, record_type, depth):
        if depth >= max_depth:
            return []

        resolved_items = []

        if name not in unique_servers:
            unique_servers.add(name)
            resolved_items.append(f"DOMAIN:{name}")

        if udp_servers:
            for udp_server in udp_servers:
                resolver = dns.resolver.Resolver()
                resolver.nameservers = [udp_server["host"]]
                resolver.port = udp_server["port"]
                resolver.lifetime = DNS_RESOLVER_LIFETIME
                resolver.timeout = DNS_RESOLVER_TIMEOUT

                try:
                    answers = resolver.resolve(name, record_type)
                    for rdata in answers:
                        ip_or_cname = rdata.to_text().strip(".")
                        if ip_or_cname not in unique_servers:
                            unique_servers.add(ip_or_cname)
                            if record_type == "CNAME":
                                resolved_items.append(f"DOMAIN:{ip_or_cname}")
                                resolved_items.extend(
                                    resolve_single(ip_or_cname, "A", depth + 1)
                                )
                                resolved_items.extend(
                                    resolve_single(ip_or_cname, "AAAA", depth + 1)
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
                    continue
                except Exception as exc:
                    udp_server_display = format_host_with_port(
                        udp_server["host"], udp_server["port"]
                    )
                    logger.warning(
                        "Unexpected error resolving %s (%s) via %s: %s",
                        name,
                        record_type,
                        udp_server_display,
                        exc,
                    )
                    continue

        for doh_url in doh_servers:
            try:
                ips = resolve_doh(name, doh_url, max_depth=max_depth - depth)
                for ip in ips:
                    if ip not in unique_servers:
                        unique_servers.add(ip)
                        if not is_private_ip(ip):
                            resolved_items.append(ip)
            except Exception as exc:
                logger.warning(
                    "DoH resolution failed for %s via %s: %s",
                    name,
                    doh_url,
                    exc,
                )
                continue

        return resolved_items

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(resolve_single, domain, record_type, 0)
            for record_type in ["A", "AAAA", "CNAME"]
        ]
        for future in concurrent.futures.as_completed(futures):
            try:
                results.extend(future.result())
            except Exception as exc:
                logger.warning("Error while collecting DNS results for %s: %s", domain, exc)

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

            except Exception as exc:
                logger.warning("Error resolving %s: %s", server, exc)

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

    contents: ContentFile | None = None
    file_exists = False
    try:
        repo_contents = repo.get_contents(file_path, ref=branch)
        if isinstance(repo_contents, list):
            raise ValueError(
                f"Path '{file_path}' refers to a directory, please provide a file path"
            )
        contents = repo_contents
        file_exists = True
    except ValueError:
        raise
    except Exception as exc:
        if "Not Found" in str(exc):
            file_exists = False
        else:
            raise

    with open(filename, "r", encoding="utf-8") as file:
        file_content = file.read()

    if file_exists:
        if contents is None:
            raise RuntimeError("Expected existing file contents but none were retrieved")
        repo.update_file(
            contents.path,
            f"Update {rename}",
            file_content,
            contents.sha,
            branch=branch,
        )
        return

    repo.create_file(
        file_path,
        f"Add {rename}",
        file_content,
        branch=branch,
    )


def process_yaml_with_js(yaml_file_path, js_file_path):
    with open(js_file_path, "r", encoding="utf-8") as js_file:
        js_code = js_file.read()

    with tempfile.NamedTemporaryFile(
        "w", delete=False, suffix=".yaml", encoding="utf-8"
    ) as temp_processed_yaml:
        temp_processed_yaml_path = temp_processed_yaml.name

    safe_yaml_path = yaml_file_path.replace("\\", "/")
    safe_temp_path = temp_processed_yaml_path.replace("\\", "/")

    node_script = f"""
    const fs = require('fs');
    const path = require('path');
    const yaml = require('js-yaml');
    const iconv = require('iconv-lite');
    const yamlRoot = path.dirname(require.resolve('js-yaml'));
    const loadYamlType = (typeName) => require(path.join(yamlRoot, 'lib', 'type', typeName));
    const common = require(path.join(yamlRoot, 'lib', 'common'));
    const YAML_FLOAT_PATTERN = new RegExp(
        '^(?:[-+]?(?:[0-9][0-9_]*)\\\\.[0-9_]*(?:[eE][-+]?[0-9]+)?' +
        '|\\\\.[0-9_]+(?:[eE][-+]?[0-9]+)?' +
        '|[-+]?\\\\.(?:inf|Inf|INF)' +
        '|\\\\.(?:nan|NaN|NAN))$'
    );

    function resolveYamlFloat(data) {{
        return data !== null && YAML_FLOAT_PATTERN.test(data) && data[data.length - 1] !== '_';
    }}

    function constructYamlFloat(data) {{
        let value = data.replace(/_/g, '').toLowerCase();
        let sign = value[0] === '-' ? -1 : 1;

        if ('+-'.includes(value[0])) {{
            value = value.slice(1);
        }}

        if (value === '.inf') {{
            return sign === 1 ? Number.POSITIVE_INFINITY : Number.NEGATIVE_INFINITY;
        }}
        if (value === '.nan') {{
            return NaN;
        }}
        return sign * parseFloat(value, 10);
    }}

    function representYamlFloat(object, style) {{
        if (isNaN(object)) {{
            if (style === 'uppercase') return '.NAN';
            if (style === 'camelcase') return '.NaN';
            return '.nan';
        }}
        if (object === Number.POSITIVE_INFINITY) {{
            if (style === 'uppercase') return '.INF';
            if (style === 'camelcase') return '.Inf';
            return '.inf';
        }}
        if (object === Number.NEGATIVE_INFINITY) {{
            if (style === 'uppercase') return '-.INF';
            if (style === 'camelcase') return '-.Inf';
            return '-.inf';
        }}
        if (common.isNegativeZero(object)) {{
            return '-0.0';
        }}

        const rendered = object.toString(10);
        return /^[-+]?[0-9]+e/.test(rendered) ? rendered.replace('e', '.e') : rendered;
    }}

    const STRICT_FLOAT_TYPE = new yaml.Type('tag:yaml.org,2002:float', {{
        kind: 'scalar',
        resolve: resolveYamlFloat,
        construct: constructYamlFloat,
        predicate: (object) =>
            Object.prototype.toString.call(object) === '[object Number]' &&
            (object % 1 !== 0 || common.isNegativeZero(object)),
        represent: representYamlFloat,
        defaultStyle: 'lowercase'
    }});

    const STRING_SAFE_SCHEMA = yaml.FAILSAFE_SCHEMA.extend({{
        implicit: [
            loadYamlType('null'),
            loadYamlType('bool'),
            loadYamlType('int'),
            STRICT_FLOAT_TYPE,
            loadYamlType('timestamp'),
            loadYamlType('merge')
        ],
        explicit: [
            loadYamlType('binary'),
            loadYamlType('omap'),
            loadYamlType('pairs'),
            loadYamlType('set')
        ]
    }});

    {js_code}

    function processYaml() {{
        const yamlInput = fs.readFileSync('{safe_yaml_path}');
        let yamlStr = iconv.decode(yamlInput, 'utf-8');
        
        if (yamlStr.charCodeAt(0) === 0xFEFF) {{
            yamlStr = yamlStr.slice(1);
        }}

        const config = yaml.load(yamlStr, {{ schema: STRING_SAFE_SCHEMA }});
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
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "Node script failed while processing %s with %s: %s",
            yaml_file_path,
            js_file_path,
            exc,
        )
        raise
    finally:
        remove_file(temp_node_file_path)
        remove_file(temp_processed_yaml_path)


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/listget", methods=["GET"])
def listget():
    provided_api_key = request.args.get("api_key", "")
    source = request.args.get("source")
    field = request.args.get("field", "proxies.server")
    repo = request.args.get("repo")
    token = request.args.get("token")
    branch = request.args.get("branch", "main")
    path = request.args.get("path", "")
    filename = request.args.get("filename", "yaml.list")
    dns_server_values = request.args.getlist("dns_servers")
    max_depth_str = request.args.get("max_depth", 8)
    resolve_domains = request.args.get("resolve_domains", "false").lower() == "true"

    if not is_authorized_request(provided_api_key):
        return jsonify({"error": "Invalid API key"}), 403

    if not source:
        return jsonify({"error": "Missing source parameter"}), 400

    max_depth = parse_max_depth(max_depth_str)

    try:
        proxies = build_proxy_config(request.args.get("proxy", ""))
        temp_source_path = None
        try:
            temp_source_path = download_file(source, proxies=proxies)
            with open(temp_source_path, "r", encoding="utf-8") as file:
                data = yaml.safe_load(file)
        except requests.exceptions.Timeout:
            return jsonify({"error": "Request timed out while fetching source"}), 408
        except requests.exceptions.SSLError:
            return jsonify({"error": "SSL verification failed"}), 495
        except requests.exceptions.RequestException as exc:
            logger.warning("Network error while fetching source %s: %s", source, exc)
            return (
                jsonify(
                    {
                        "error": "Network error while fetching source, please retry or provide a mirror URL",
                        "detail": str(exc),
                    }
                ),
                502,
            )
        except yaml.YAMLError as exc:
            return jsonify({"error": f"Invalid YAML format: {exc}"}), 400
        except Exception as exc:
            return jsonify({"error": f"Unexpected error: {exc}"}), 500
        finally:
            remove_file(temp_source_path)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"Unexpected error: {exc}"}), 500

    port_requested = field.endswith(".port")
    effective_field = field[: -len(".port")] if port_requested else field
    server_port_map = (
        extract_server_port_map(data, max_depth=max_depth) if port_requested else {}
    )

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
        dns_servers = normalize_dns_server_entries(dns_server_values) or DEFAULT_DNS_SERVERS
        current_dns_server = ""

        try:
            filter_doh_servers(dns_servers)
            for dns_server in dns_servers:
                current_dns_server = dns_server
                if is_doh_server(dns_server):
                    continue
                parsed_dns_server = parse_udp_dns_server(dns_server)
                socket.getaddrinfo(
                    parsed_dns_server["host"],
                    parsed_dns_server["port"],
                    socket.AF_UNSPEC,
                    socket.SOCK_DGRAM,
                )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except socket.gaierror as exc:
            return jsonify({"error": f"Invalid DNS server '{current_dns_server}': {exc}"}), 400

        temp_filename = generate_server_list(
            servers,
            dns_servers,
            max_depth=max_depth,
            server_port_map=server_port_map if port_requested else None,
        )
    else:
        with tempfile.NamedTemporaryFile(
            "w", delete=False, suffix=".txt", encoding="utf-8"
        ) as file:
            temp_filename = file.name
            for server in servers:
                if port_requested:
                    ports = server_port_map.get(str(server), [])
                    for port in ports:
                        file.write(f"{format_host_with_port(server, port)}\n")
                else:
                    file.write(f"{server}\n")

    if repo and token:
        try:
            upload_to_github(temp_filename, repo, token, branch, path, filename)
            target_path = posixpath.join(path, filename) if path else filename
            return jsonify(
                {"message": f"Uploaded {filename} to {repo}@{branch}:{target_path}"}
            )
        except Exception as exc:
            return (
                jsonify({"error": f"Failed to upload to GitHub: {exc}"}),
                500,
            )
        finally:
            remove_file(temp_filename)

    return send_download(temp_filename, filename)


@app.route("/yamlprocess", methods=["GET"])
def yamlprocess():
    provided_api_key = request.args.get("api_key", "")
    source_url = request.args.get("source")
    merge_url = request.args.get("merge")
    filename = request.args.get("filename")

    if not is_authorized_request(provided_api_key):
        return jsonify({"error": "Invalid API key"}), 403

    if not source_url or not merge_url:
        return jsonify({"error": "Missing source or merge URL"}), 400

    try:
        proxies = build_proxy_config(request.args.get("proxy", ""))
        temp_yaml_file_path = None
        temp_js_file_path = None
        try:
            temp_yaml_file_path = download_file(source_url, proxies=proxies)
            temp_js_file_path = download_file(merge_url, proxies=proxies)

            process_yaml_with_js(temp_yaml_file_path, temp_js_file_path)

            download_filename = (
                filename or os.path.basename(source_url) or "processed.yaml"
            )
            response = send_download(temp_yaml_file_path, download_filename)
            temp_yaml_file_path = None
            return response
        finally:
            remove_file(temp_js_file_path)
            remove_file(temp_yaml_file_path)

    except requests.exceptions.Timeout:
        return jsonify({"error": "Request timed out while fetching files"}), 408
    except requests.exceptions.SSLError:
        return jsonify({"error": "SSL verification failed"}), 495
    except requests.exceptions.RequestException as exc:
        logger.warning("Network error while fetching files: %s", exc)
        return (
            jsonify(
                {
                    "error": "Network error while fetching files, please retry or provide a mirror URL",
                    "detail": str(exc),
                }
            ),
            502,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"Error processing files: {exc}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=19527)
