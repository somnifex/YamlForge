<p align="center">
<img src="../assets/yamlforge.png" alt="YamlForge" width="200">
</p>
<h1 align="center">
  YamlForge
</h1>

<p align="center">
 <a href="../README.md">English</a> | <a href="README.cn.md">简体中文</a>
</p>

<p align="center">
  <a href="https://github.com/somnifex/yamlforge/blob/main/LICENSE"><img src="../assets/GPL-3.0License.svg" alt="License"></a>
  <a href="https://github.com/somnifex/yamlforge/pulls"><img src="../assets/PRs-welcome-brightgreen.svg" alt="PRs Welcome"></a>
</p>

YamlForge 主要做两件事：从远程 YAML 里提取字段，或者在下载 YAML 后再跑一段 JavaScript 脚本继续处理。结果可以直接下载，也可以顺手推到 GitHub。

## 它能做什么

- 从远程 YAML 中提取字段，比如 `proxies.server`
- 用 UDP DNS 或 DoH 解析域名
- 对 YAML 执行一个 JavaScript `main(config)` 处理函数
- 直接下载结果，或者把生成的文件上传到 GitHub

## 快速开始

### Docker

```bash
docker run -d --restart unless-stopped \
  --name yamlforge \
  -p 19527:19527 \
  -e API_KEY=your_api_key \
  utopeadia/yamlforge:latest
```

如果服务会暴露到公网，建议先设置 `API_KEY`。需要多个密钥时，可以用英文逗号分隔。

### 自己构建镜像

1. 克隆仓库。

   ```bash
   git clone https://github.com/somnifex/YamlForge.git
   cd YamlForge
   ```

2. 构建镜像。

   ```bash
   docker build -t yamlforge .
   ```

3. 运行容器。

   ```bash
   docker run -d --restart unless-stopped \
     --name yamlforge \
     -p 19527:19527 \
     -e API_KEY=your_api_key \
     yamlforge
   ```

### 本地运行

本地运行需要 Python 3.10+，同时机器上要能直接调用 Node.js。

1. 克隆仓库。

   ```bash
   git clone https://github.com/somnifex/YamlForge.git
   cd YamlForge
   ```

2. 安装 Python 依赖。

   ```bash
   pip install -r requirements.txt
   ```

3. 安装运行时需要的 Node 包。

   ```bash
   npm install -g js-yaml iconv-lite
   ```

4. 设置 API 密钥。

   ```bash
   export API_KEY=your_api_key
   ```

5. 启动服务。

   ```bash
   python app.py
   ```

启动后可以直接打开 `http://127.0.0.1:19527` 使用网页界面。

## 接口概览

两个接口都通过查询参数传值。如果 `source` 或 `merge` 里包含 `?`、`&` 或非 ASCII 字符，记得先做 URL 编码。

### `/listget`

这个接口用来从 YAML 提取字段，并生成 `.list` 文件。

| 参数 | 说明 | 必填 | 默认值 |
| --- | --- | --- | --- |
| `api_key` | 用于鉴权的 API 密钥 | 是 |  |
| `source` | YAML 文件地址 | 是 |  |
| `proxy` | 下载代理，例如 `http://user:pass@host:port` 或 `socks5://host:port` | 否 |  |
| `field` | 需要提取的 YAML 路径。若想把端口一起带上，可以传 `proxies.server.port`。 | 否 | `proxies.server` |
| `repo` | GitHub 仓库名，格式为 `owner/repo` | 否 |  |
| `token` | GitHub 个人访问令牌 | 否 |  |
| `branch` | GitHub 分支 | 否 | `main` |
| `path` | GitHub 仓库内的目标目录 | 否 | 仓库根目录 |
| `filename` | 输出文件名 | 否 | `yaml.list` |
| `dns_servers` | 逗号分隔的 DNS 或 DoH 列表。支持 `IP`、`host`、`IP:port`、`host:port`、`[IPv6]:port` 和 DoH URL，仅在 `resolve_domains=true` 时生效。 | 否 | `223.5.5.5,8.8.8.8` |
| `max_depth` | 读取字段或解析域名时允许的最大深度 | 否 | `8` |
| `resolve_domains` | 是否先解析提取到的域名，再写入结果文件 | 否 | `false` |

示例：

```text
http://127.0.0.1:19527/listget?api_key=your_api_key&source=YOUR_YAML_URL&field=proxies.server&filename=yaml.list
```

如果还需要解析域名，再补上 `resolve_domains=true`，必要时再传 `dns_servers=...`。

### `/yamlprocess`

这个接口会先下载 YAML，再执行一段 JavaScript 脚本处理内容。

| 参数 | 说明 | 必填 | 默认值 |
| --- | --- | --- | --- |
| `api_key` | 用于鉴权的 API 密钥 | 是 |  |
| `source` | 基础 YAML 文件地址 | 是 |  |
| `merge` | JavaScript 处理脚本地址 | 是 |  |
| `filename` | 输出文件名 | 否 | `source` 的文件名 |
| `proxy` | 下载代理，例如 `http://user:pass@host:port` 或 `socks5://host:port` | 否 |  |

示例：

```text
http://127.0.0.1:19527/yamlprocess?api_key=your_api_key&source=YOUR_BASE_YAML_URL&merge=YOUR_MERGE_JS_URL&filename=processed.yaml
```

### JavaScript 脚本约定

脚本里需要定义一个 `main` 函数。YamlForge 会把解析后的 YAML 对象传进去，再把 `main` 的返回值写回文件。

## 运行时参数

下面这些环境变量主要用来调整网络行为和生产环境下的容器配置。

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `API_KEY` | 允许访问接口的 API 密钥列表，多个值用英文逗号分隔 | `""` |
| `GUNICORN_TIMEOUT` | Gunicorn worker 超时时间，单位秒 | `300` |
| `GUNICORN_WORKERS` | Docker 镜像里 Gunicorn worker 数量 | `2` |
| `DOWNLOAD_TIMEOUT` | 使用 `requests` 下载远程文件时的超时时间 | `600` |
| `DNS_RESOLVER_TIMEOUT` | 单次 DNS 查询超时时间 | `5` |
| `DNS_RESOLVER_LIFETIME` | 一次 DNS 解析尝试的总生命周期 | `10` |
| `RETRY_BACKOFF_FACTOR` | 网络请求重试退避系数 | `1` |
| `RETRY_TOTAL` | 网络请求最大重试次数 | `5` |
| `MAX_WORKERS` | 并发解析 DNS 时使用的线程数 | `10` |
| `DOWNLOAD_ATTEMPTS` | 单次下载任务的最大尝试次数 | `5` |
| `DOWNLOAD_RETRY_WAIT` | 下载重试之间的基础等待时间 | `2` |

示例：

```bash
docker run -d -p 19527:19527 \
  -e API_KEY="your-secret-key" \
  -e GUNICORN_TIMEOUT=600 \
  -e GUNICORN_WORKERS=4 \
  -e DOWNLOAD_TIMEOUT=1200 \
  -e MAX_WORKERS=20 \
  yamlforge:latest
```

## 安全建议

- 如果服务会暴露到公网，先设置 `API_KEY`。
- 生产环境最好放在 HTTPS 之后。
- GitHub 令牌尽量只给最小权限，也不要直接泄露到公开配置里。
- 如果 YAML 内容比较敏感，最好自己部署，不要把数据交给来路不明的第三方实例。

## 许可证

[GPLv3](../LICENSE)
