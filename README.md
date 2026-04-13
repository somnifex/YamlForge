<p align="center">
<img src="assets/yamlforge.png" alt="YamlForge" width="200">
</p>
<h1 align="center">
  YamlForge
</h1>

<p align="center">
  <a href="README.md">English</a> | <a href="docs/README.cn.md">简体中文</a>
</p>

<p align="center">
  <a href="https://github.com/somnifex/yamlforge/blob/main/LICENSE"><img src="assets/GPL-3.0License.svg" alt="License"></a>
  <a href="https://github.com/somnifex/yamlforge/pulls"><img src="assets/PRs-welcome-brightgreen.svg" alt="PRs Welcome"></a>
</p>

YamlForge pulls a remote YAML file, extracts the part you need, and returns the result as a downloadable file. If you want, it can also resolve domains, run a JavaScript transform, and upload the generated list to GitHub.

## What it does

- Extract a field such as `proxies.server` from a remote YAML file
- Resolve domains with UDP DNS servers or DoH endpoints
- Run a JavaScript `main(config)` transform against a YAML file
- Download the result directly or push it to GitHub

## Quick start

### Docker

```bash
docker run -d --restart unless-stopped \
  --name yamlforge \
  -p 19527:19527 \
  -e API_KEY=your_api_key \
  utopeadia/yamlforge:latest
```

Set `API_KEY` if the service is reachable from a public network. Multiple keys can be passed as a comma-separated list.

### Build the image yourself

1. Clone the repository.

   ```bash
   git clone https://github.com/somnifex/YamlForge.git
   cd YamlForge
   ```

2. Build the image.

   ```bash
   docker build -t yamlforge .
   ```

3. Run the container.

   ```bash
   docker run -d --restart unless-stopped \
     --name yamlforge \
     -p 19527:19527 \
     -e API_KEY=your_api_key \
     yamlforge
   ```

### Run locally

YamlForge expects Python 3.10+ and Node.js in `PATH`.

1. Clone the repository.

   ```bash
   git clone https://github.com/somnifex/YamlForge.git
   cd YamlForge
   ```

2. Install Python dependencies.

   ```bash
   pip install -r requirements.txt
   ```

3. Install the Node packages used at runtime.

   ```bash
   npm install -g js-yaml iconv-lite
   ```

4. Set an API key.

   ```bash
   export API_KEY=your_api_key
   ```

5. Start the app.

   ```bash
   python app.py
   ```

Open `http://127.0.0.1:19527` to use the web interface.

## API overview

Both endpoints use query parameters. If `source` or `merge` contains `?`, `&`, or non-ASCII characters, URL-encode it before sending the request.

### `/listget`

Use this endpoint when you want a `.list` file from a YAML field.

| Parameter | Description | Required | Default |
| --- | --- | --- | --- |
| `api_key` | API key for authentication | Yes |  |
| `source` | URL of the YAML file | Yes |  |
| `proxy` | Download proxy, for example `http://user:pass@host:port` or `socks5://host:port` | No |  |
| `field` | YAML path to extract. Use `proxies.server.port` if you want the server value with its port appended. | No | `proxies.server` |
| `repo` | GitHub repository in `owner/repo` format | No |  |
| `token` | GitHub personal access token | No |  |
| `branch` | GitHub branch | No | `main` |
| `path` | Target directory inside the GitHub repository | No | repository root |
| `filename` | Output filename | No | `yaml.list` |
| `dns_servers` | Comma-separated DNS servers or DoH URLs. Supports `IP`, `host`, `IP:port`, `host:port`, `[IPv6]:port`, and DoH URLs. Only used when `resolve_domains=true`. | No | `223.5.5.5,8.8.8.8` |
| `max_depth` | Maximum traversal depth when reading fields or resolving domains | No | `8` |
| `resolve_domains` | Resolve extracted domain names before writing the list | No | `false` |

Example:

```text
http://127.0.0.1:19527/listget?api_key=your_api_key&source=YOUR_YAML_URL&field=proxies.server&filename=yaml.list
```

If you also want domain resolution, add `resolve_domains=true` and optionally `dns_servers=...`.

### `/yamlprocess`

Use this endpoint when you want to apply a JavaScript merge or transform script to a YAML file.

| Parameter | Description | Required | Default |
| --- | --- | --- | --- |
| `api_key` | API key for authentication | Yes |  |
| `source` | URL of the base YAML file | Yes |  |
| `merge` | URL of the JavaScript transform script | Yes |  |
| `filename` | Output filename | No | source basename |
| `proxy` | Download proxy, for example `http://user:pass@host:port` or `socks5://host:port` | No |  |

Example:

```text
http://127.0.0.1:19527/yamlprocess?api_key=your_api_key&source=YOUR_BASE_YAML_URL&merge=YOUR_MERGE_JS_URL&filename=processed.yaml
```

### JavaScript transform contract

Your script should define a `main` function. YamlForge passes the parsed YAML object into that function and expects the modified object back.

## Runtime settings

These environment variables are useful when you need to tune network behavior or the production container.

| Variable | Description | Default |
| --- | --- | --- |
| `API_KEY` | Comma-separated list of accepted API keys | `""` |
| `GUNICORN_TIMEOUT` | Gunicorn worker timeout in seconds | `300` |
| `GUNICORN_WORKERS` | Number of Gunicorn workers in the Docker image | `2` |
| `DOWNLOAD_TIMEOUT` | Timeout for downloading remote files with `requests` | `600` |
| `DNS_RESOLVER_TIMEOUT` | Timeout for a single DNS query | `5` |
| `DNS_RESOLVER_LIFETIME` | Total lifetime of a DNS resolution attempt | `10` |
| `RETRY_BACKOFF_FACTOR` | Retry backoff factor for network requests | `1` |
| `RETRY_TOTAL` | Maximum retry count for network requests | `5` |
| `MAX_WORKERS` | Number of concurrent DNS resolution workers | `10` |
| `DOWNLOAD_ATTEMPTS` | Maximum attempts for one download job | `5` |
| `DOWNLOAD_RETRY_WAIT` | Base wait time between download retries | `2` |

Example:

```bash
docker run -d -p 19527:19527 \
  -e API_KEY="your-secret-key" \
  -e GUNICORN_TIMEOUT=600 \
  -e GUNICORN_WORKERS=4 \
  -e DOWNLOAD_TIMEOUT=1200 \
  -e MAX_WORKERS=20 \
  yamlforge:latest
```

## Security notes

- Set `API_KEY` before exposing the service to a public network.
- Put the service behind HTTPS in production.
- Keep GitHub personal access tokens private and scope them narrowly.
- If the YAML data is sensitive, run your own instance instead of using an unknown third-party deployment.

## License

[GPLv3](LICENSE)
