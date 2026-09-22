# DICOM 检查匿名化工作台

一个纯本地运行的 Web 工作台：上传一组 DICOM 影像 → 选择匿名化配置 → **预览逐字段标签变更** → 通过「残留标识门禁」后一键打包下载匿名化 ZIP。无需任何第三方账号，不向外部发送数据。

- **后端**：FastAPI + [pydicom](https://pydicom.github.io/)
- **前端**：原生 HTML/CSS/JS 单页应用（无 CDN、无构建步骤）
- **规范**：遵循 DICOM PS3.15 Annex E「基础应用保密配置文件」及标准保留选项
- **交付**：Dockerfile + docker-compose，单条命令启动

---

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
# 构建镜像并在前台启动（首次需要拉取 python:3.11-slim，仅本机构建，不登录任何账号）
docker compose up --build

# 或后台运行
docker compose up --build -d
docker compose logs -f          # 查看日志
docker compose down             # 停止并移除容器（命名卷中的数据保留）
```

然后浏览器打开 <http://localhost:8000>。容器自带健康检查，`docker ps` 中可看到 `healthy` 状态（探测 `/api/health`）。

常用自定义（直接修改 `docker-compose.yml` 或使用环境变量覆盖，无需账号）：

- **端口**：把 `ports` 改为 `"18000:8000"` 后访问 <http://localhost:18000>；
- **盐值**：修改 `DICOMBENCH_SALT`（决定伪名与新 UID，建议部署时改成随机串）；
- **存储位置**：把命名卷 `dicombench-data:/data` 换成本机目录，如 `./data:/data`；
- **上传上限 / 任务保留时长**：调整 `DICOMBENCH_MAX_UPLOAD_MB`、`DICOMBENCH_JOB_TTL_SECONDS`。

也可以用命令行覆盖，例如：

```bash
DICOMBENCH_SALT="$(openssl rand -hex 32)" docker compose up --build -d
```

### 方式二：本地 Python（3.10+）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

打开 <http://localhost:8000>。

### 方式三：仅 Docker（不使用 Compose）

```bash
docker build -t dicombench .

# 使用命名卷持久化 /data，并显式注入盐值与端口
docker run -d --name dicombench \
  -p 8000:8000 \
  -v dicombench-data:/data \
  -e DICOMBENCH_SALT="change-me-to-a-random-secret" \
  --restart unless-stopped \
  dicombench

docker logs -f dicombench          # 跟踪日志
docker stop dicombench && docker rm dicombench   # 停止并删除容器
```

容器内不包含任何外部服务凭据，运行期不会主动发起第三方网络请求。

---

## 使用流程

1. **上传**：拖放或选择影像文件（支持一次选择整个检查目录，保留子目录结构）。非 DICOM 文件会自动识别并跳过。
2. **配置**：选择预设方案（轻度 / 标准 / 推荐 / 严格），可逐项微调 8 个选项，可自定义伪名盐值。
3. **预览**：
   - 统计磁贴（删除/置空/伪名替换/UID 重写/日期平移/剥离私有标签数量）
   - **逐字段变更清单**：标签号、名称、VR、动作、原值 → 新值，支持搜索、按动作过滤、分页
   - 每个文件的 Patient / Study / Series / SOP UID 原 → 新映射
   - 患者伪名表与每患者日期平移天数
   - 警告（悬挂引用、`Burned In Annotation`、重复 SOP UID 等）
   - **残留标识门禁结果**：显示扫描文件数、比对标识数、检查值数与命中计数
4. **执行与下载**：执行后再次运行门禁扫描，**通过时**才打包为 ZIP（内部以 `新StudyUID/新SeriesUID/新SOPUID.dcm` 组织），并附 `anonymization-report.json`（只含新标识、统计与门禁摘要，不含原值）；**命中时阻止 ZIP 生成/下载**并逐项列出命中文件、标签与命中类型，可调整配置后重试。

---

## 匿名化策略

### 患者级稳定伪名

`PatientName`、`PatientID` 由盐值 + 患者身份指纹（ID / 姓名 / 出生日期）经 **SHA-256** 确定性派生：

- 同一患者在**任意批次、任意时间**始终得到相同伪名，便于纵向研究关联；
- 更换盐值即得到完全不同的伪名空间，无法反推；
- 伪名形如 `ANON^KQML5SMX` / `ANON-3F9A21C7B0E1`。

### UID 一致性重写（检查关系不变）

- 所有身份型 UID（Study / Series / SOP / Frame of Reference / Concatenation / Dimension Organization 等）替换为 `2.25.<128位>` 根下的确定性 UID；
- 映射是 **盐值 + 原 UID** 的纯函数：同一原 UID 在批内任意位置（含序列深层引用）都映射为同一新 UID；
- 因此同一检查的多张影像仍共享同一个新 Study / Series UID，**序列内/跨序列引用**（`ReferencedSOPInstanceUID`、`ReferencedFrameOfReferenceUID`、Source Image 链、RT 引用链等）在递归遍历序列后**继续指向正确目标**；
- SOP Class UID、Transfer Syntax UID、Implementation Class UID 等结构/注册型 UID 保持不变；
- 引用的目标不在本次上传中时给出**悬挂引用警告**（映射仍按规则重写）。

### 日期处理

- 默认（标准/严格）：清空所有 DA / DT / TM 属性；
- `keep_dates`（推荐预设）：每个患者向过去平移 **1–364 天中的固定天数**（由盐值确定性派生）。同一患者的所有日期使用同一偏移，**检查间隔与时间顺序完全保持**；DT 值保留时刻与时区后缀，TM 保留；无法解析的日期置空并告警；
- `PatientBirthDate` 在保留人口学特征时按 PS3.15 泛化为 `<出生年>0101`（保留年龄段，不参与平移）。

### 其他

- **私有标签**（odd group / private creator）无条件剥离（基础保密配置文件要求）；
- `strip_overlays`：移除 60xx 覆盖层、50xx 曲线与图形注释序列（可能烧录文字）；
- 人员姓名（PN）、机构、设备、医护、Accession、描述/注释等按所选选项保留或清空；
- 写入去标识声明：`PatientIdentityRemoved = YES`、`De-identificationMethod` 及标准代码序列（113100 等）；
- `Burned In Annotation = YES` / `Recognizable Visual Features = YES` 时给出像素级人工核查警告（工具**不修改像素**）；
- DICOMDIR 跳过并警告（其记录基于原 UID，需用介质工具重建）。

### 残留标识门禁（Residual Identifier Gate）

匿名化规则无法保证 100% 覆盖（例如保留的自由文本、设备字段中可能写有姓名或 UID）。因此每次**预览与执行后**都会运行门禁：

1. 从上传源收集标识：患者姓名（含 PN 各组成部分）、患者 ID，以及全部被重写的原始 Study/Series/SOP/Frame of Reference 等 UID；
2. **递归扫描每个输出 DICOM**（含 file meta 与所有序列深层条目）：
   - `UI` 属性与原始 UID 集合**精确比对**（重写后的 `2.25.*` 新 UID 不会命中）；
   - 文本类属性（PN/LO/LT/UT/SH/ST/UC/UR 等）按 NFKC 归一化、忽略大小写/空白/PN 分隔符后检索源姓名、ID 与 UID（UID 使用组件边界匹配，避免超串误报）；
3. 递归扫描 `anonymization-report.json` 的每个字符串叶子节点（JSON Pointer 定位）；
4. 为避免误报，过短/过于通用的片段（单字名、2 位 ID 等）不纳入比对。

- **命中**：返回 `422`，任务状态为 `blocked`，**不生成 ZIP、下载接口拒绝服务（409）**；响应中给出每项命中的 `文件 / 标签（含序列路径）/ 命中类型（患者姓名·患者 ID·原始 UID）/ 命中文本截断`。预览页与处理失败时都会展示命中清单，调整选项后可直接重新执行（重跑会先清理上一次产物）。
- **通过**：预览页/结果页显示「门禁通过」及命中计数（0）；摘要写入报告的 `residual_identifier_gate` 字段（含扫描文件数、比对标识数、检查值数、命中数；**不含任何原始值或命中文本**）。

> 门禁同样无法识别**像素中烧录的文字**，发布前仍需按 `Burned In Annotation` 警告人工核查。

### 预设方案

| 方案 | 适用 | 要点 |
|---|---|---|
| 轻度 | 院内可信流转 | 仅改身份字段与 UID，保留机构/设备/日期/描述 |
| 标准 | 一般外发 | PS3.15 基础方案，日期清空 |
| **推荐** | 科研外发 | 日期固定偏移（纵向间隔一致）、保留年龄/性别与协议名、剥离覆盖层 |
| 严格 | 最大化去标识 | 清空一切日期/特征/自由文本、剥离私有标签与覆盖层 |

> **注意**：任何 DICOM 标签层面的去标识都无法处理**像素中烧录的文字**（姓名、床号、面部特征）。本工具会读取 `(0028,0301)` 等标记提示风险，但发布前仍需人工核查影像内容。

---

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DICOMBENCH_SALT` | 内置默认盐 | 伪名/UID/日期偏移的派生盐，生产环境建议修改 |
| `DICOMBENCH_DATA_DIR` | 系统临时目录 | 上传与 ZIP 的存储目录（compose 中为 `/data` 卷） |
| `DICOMBENCH_MAX_UPLOAD_MB` | `2048` | 单次上传总量上限（MB），`0` 不限 |
| `DICOMBENCH_JOB_TTL_SECONDS` | `3600` | 任务文件保留时长，过期自动清理 |
| `DICOMBENCH_PORT` | `8000` | 仅本地 `uvicorn` 启动方式使用 |

任务文件仅保存在运行服务器的本地磁盘（容器卷）中，进程不会向任何第三方地址发起网络请求。

---

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/health` | 健康检查 |
| `GET` | `/api/profiles` | 预设方案与选项定义 |
| `POST` | `/api/jobs` | 上传文件（multipart，字段名 `files`，可多个） |
| `POST` | `/api/jobs/{id}/preview` | 计算变更预览（不落盘）；门禁命中返回 `422`（响应体含 `detail.gate.hits`） |
| `POST` | `/api/jobs/{id}/process` | 执行匿名化并生成 ZIP；门禁命中返回 `422`，不产生 ZIP |
| `GET` | `/api/jobs/{id}/status` | 任务状态（`uploaded|previewed|processing|done|blocked|error`）与门禁摘要 |
| `GET` | `/api/jobs/{id}/download` | 下载 ZIP（仅 `done` 状态可用，否则 `409`） |
| `DELETE` | `/api/jobs/{id}` | 立即删除任务及其文件 |

预览/执行请求体示例：

```json
{
  "profile": "recommended",
  "keep_dates": true,
  "keep_chars": true,
  "strip_overlays": true,
  "salt": null
}
```

所有选项字段省略或传 `null` 时沿用所选预设；显式传 `true`/`false` 覆盖预设。

API 文档（Swagger UI）：<http://localhost:8000/docs>。

---

## 项目结构

```
app/
  main.py            # FastAPI 路由 / 静态页面
  jobs.py            # 任务生命周期、预览调度、门禁调度、ZIP 打包与报告
  anonymizer.py      # 伪名 / UID 映射 / 日期平移 / 递归序列遍历引擎
  gate.py            # 残留标识门禁：源标识收集与输出 DICOM/报告递归比对
  dicom_catalog.py   # PS3.15 标签规则表与 4 个预设方案
  schemas.py         # 请求/响应模型
  config.py          # 环境变量配置
  static/            # 单页前端（index.html / app.js / style.css）
tests/               # 引擎、门禁与端到端 HTTP 测试（pytest）
Dockerfile
docker-compose.yml
requirements.txt
```

## 开发与测试

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q          # 30 项测试：UID 一致性、引用遍历、确定性、日期间隔、残留标识门禁等
```
