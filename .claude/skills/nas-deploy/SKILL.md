---
name: nas-deploy
description: 把当前代码构建成 Docker 镜像并更新 QNAP NAS 上的 movieclaw 生产容器（分支/预览部署）。当用户要求"构建并更新 NAS docker"、"部署到 NAS"、"NAS 上看看效果"时使用。正式发版请用 release 技能，两者不要混用。
---

# movieclaw NAS 部署（本地构建直传）

把**已提交的代码**构建成 linux/amd64 镜像，直传 NAS 更新容器。适用于功能分支
预览部署；带版本号、打 tag、动公共 `latest` 的正式发版走 `.claude/skills/release/SKILL.md`。

## 〇、判定与硬约束

- **改动必须先提交**：构建上下文来自 `git archive HEAD`，未提交的改动不会进镜像。
- **动了运行时依赖**（pyproject dependencies、Node 大版本、Dockerfile 系统包/
  基础镜像、entrypoint 契约）**必须 bump `docker/runtime-version`**——但这种改动
  属于正式发版范畴，不要用本流程偷偷上线。
- **分支镜像绝不推 Docker Hub**，尤其不能覆盖公共 `latest`（这是开源项目的
  公共镜像）。CI 的 docker-image workflow 对预发布 tag（含 `-`）只打版本号标签，
  将来若走 CI 需利用这一点。

## 一、环境事实（已固化，不要重复探测）

- NAS：QNAP，`ssh nas`（192.168.31.10，用户 admin，sudo 免密）。**x86_64**；
  本机 Docker Desktop 也是 amd64，无需交叉构建。
- NAS docker CLI 不在 PATH：`DOCKER=/share/CACHEDEV1_DATA/.qpkg/container-station/bin/docker`，
  所有命令 `sudo -n $DOCKER …`（查询也要 sudo）。
- compose 项目 `/share/Container/movieclaw`，容器 `movieclaw`，镜像引用
  `movieclaw/movieclaw:latest`，端口 8096→3000。`data/` 子目录是持久化数据
  （数据库、更新 overlay、配置），任何操作不得删除重建。
- 容器内没有 curl/wget，健康检查从 NAS 宿主机发：
  `curl http://127.0.0.1:8096/api/v1/health`（**必须带 `/api/v1`**，裸 `/api/health` 404）。
- TMDB 构建参数的来源：仓库 `.env`（若存在）或本机演示容器
  `docker inspect movieclaw --format '{{range .Config.Env}}{{println .}}{{end}}'`；
  不要把 key 回显到日志。

## 二、为什么不走 CI / Docker Hub

仓库 `duchanghexuhe/movieclaw` 的 Actions variables/secrets 全空（
`DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN`/`TMDB_API_KEY` 均未配置），
`docker-image` workflow 手动触发必在 login 步失败；本机 docker 也只登录过
ghcr.io。所以**本地构建 → ssh 直传**是当前唯一通路。若用户补配了 CI 机密，
可改走 `gh workflow run docker-image.yml --ref <分支> -f tag=<含-的预发布tag>`。

## 三、流程

### 1. 本地构建（Windows 两大坑都在这一步）

**坑 A（CRLF）**：本机 `core.autocrlf=true`，工作区脚本全是 CRLF，`docker build .`
会把 entrypoint.sh / subtitle-smoke-test.sh 烧成坏文件（容器内 `not found`，exit 127）。
裸 `git archive` **同样**被 autocrlf 转换。必须显式关闭：

```bash
cd /g/Github/movieclaw
KEY=$(docker inspect movieclaw --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep '^TMDB_API_KEY=' | cut -d= -f2-)
git -c core.autocrlf=false archive --format=tar HEAD | docker build \
  --build-arg "TMDB_API_KEY=$KEY" \
  --build-arg "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple" \
  --build-arg "NPM_REGISTRY=https://registry.npmmirror.com" \
  --build-arg "APT_MIRROR=mirrors.tuna.tsinghua.edu.cn" \
  --shm-size=2g \
  -t movieclaw:<tag> -
```

`<tag>` 约定用 `<版本>-<分支>.<序号>`（如 `v0.24.0-netflix.1`）。
构建约 10–25 分钟，建议后台跑并轮询日志。`--shm-size=2g` 不能省（Next.js
并行 worker 靠 /dev/shm 通信，默认 64MB 会静默死锁）。

**构建完必跑字幕自检**（镜像内 PGS→SRT 运行时端到端验证，CI 同款）：

```bash
MSYS_NO_PATHCONV=1 docker run --rm \
  --entrypoint /usr/local/bin/movieclaw-subtitle-smoke-test movieclaw:<tag>
```

**坑 B（MSYS 路径改写）**：Git Bash 会把 `--entrypoint /usr/local/bin/...`
改写成 Windows 路径，导致自检报 not found——镜像其实是好的，加
`MSYS_NO_PATHCONV=1` 即可。

### 2. 直传 NAS 并加载

```bash
docker save movieclaw:<tag> | gzip -1 | \
  ssh nas "gunzip | sudo -n /share/CACHEDEV1_DATA/.qpkg/container-station/bin/docker load"
```

镜像约 1.2GB（gzip 后 ~450MB），千兆内网分钟级。

### 3. 更新容器

先记录旧镜像 ID（回滚用）：`sudo -n $DOCKER images movieclaw/movieclaw --format '{{.Tag}} {{.ID}}'`。

```bash
ssh nas "DOCKER=/share/CACHEDEV1_DATA/.qpkg/container-station/bin/docker && \
  sudo -n \$DOCKER tag movieclaw:<tag> movieclaw/movieclaw:latest && \
  cd /share/Container/movieclaw && \
  sudo -n \$DOCKER compose up -d 2>&1 | tail -3 && \
  sleep 25 && sudo -n \$DOCKER ps --filter name=movieclaw --format '{{.Status}}'"
```

compose 文件不改（NAS 始终引用本地 `latest` 标签，不涉及 Docker Hub 拉取）。

### 4. overlay 遮蔽检查（必做，最隐蔽的一步）

data 卷上可能有应用内更新的 overlay（`data/updates/versions/<ver>/`），
entrypoint 按 **current → previous → 基线** 解析代码来源，overlay 与镜像
runtime 兼容时会**压住新镜像代码**——容器健康但跑的是旧代码。启动日志必须看到：

```
[entrypoint] 启动后端 (FastAPI, 127.0.0.1:8000)……来源：baseline
[entrypoint] 启动前端 (Next.js, 127.0.0.1:3001)……来源：baseline
```

若显示 `来源：overlay vX.Y.Z`，停用 overlay 指针后重启（可逆，不删任何文件）：

```bash
ssh nas "sudo -n mv /share/Container/movieclaw/data/updates/current \
    /share/Container/movieclaw/data/updates/current.bak-<tag> && \
  sudo -n $DOCKER restart movieclaw"
```

恢复 overlay（回退旧版代码）：把 `.bak-<tag>` 改回 `current` 再 restart。
应用自带的等价 API（需登录态）：
`POST /api/v1/app/update/rollback {"target":"baseline","restore_backup":false}`，
其中 `restore_backup` 慎用——它回滚数据库，overlay 之后的线上数据会丢。

### 5. 验证清单

```
1. docker ps 显示 (healthy)                    → 部署本身成功
2. NAS 宿主机 curl /api/v1/health 得 200       → 全链路可用
3. 日志「来源：baseline」且无解密/迁移/启动错误 → 跑的确实是新镜像代码
4. 抓一个新代码标志位（如 grep 新增字段/组件）  → 确认功能在
   sudo -n $DOCKER exec movieclaw grep -rln <新字段名> /app/src
```

健康状态 `status: degraded` 是业务运行态（数据源失效等），与部署成败无关；
部署只看 `database`、`storage`、`runtime` 三段与 HTTP 200。

## 四、回滚

- **回退代码到上一个 overlay 版本**：按第 4 步把软链改回 `current` + restart。
- **回退镜像**：`sudo -n $DOCKER tag <旧镜像ID> movieclaw/movieclaw:latest`
  + `compose up -d`（旧镜像 ID 在第 3 步记录）。

## 五、已知欠账

- 仓库未配 CI 镜像发布机密（见「二」）；配好后本 skill 的构建/传输段可整体替换。
- 仓库无 `.gitattributes`，每个新克隆的 Windows 工作区都会复现坑 A；加上
  `*.sh text eol=lf`（必要时含 entrypoint.sh）可根治，届时第 1 步可改回普通
  `docker build .`，但 archive 方式仍更贴近 CI 行为，建议保留。
