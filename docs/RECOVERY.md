# 本地恢复与版本管理

本项目采用三层保护：Git 保存源码版本，项目外状态区保存业务数据库和加密密钥，项目外恢复包保存可独立还原的源码、数据与素材。

## 日常启动和停止

```bash
cd /Users/w/PycharmProjects/xianyu-auto-reply
./start.sh
```

```bash
cd /Users/w/PycharmProjects/xianyu-auto-reply
./stop.sh
```

`start.sh` 会先验证项目根标识、关键源码和 SQLite 完整性，然后创建恢复包，最后启动服务和完整性看守进程。

## 数据位置

- 项目目录：`/Users/w/PycharmProjects/xianyu-auto-reply`
- 受保护状态区：`/Users/w/xianyu-auto-reply-state`
- 恢复包目录：`/Users/w/xianyu-auto-reply-recovery`
- GitHub 源码仓库：`https://github.com/Wu636/xianyu-auto-reply`

数据库在项目内仍显示为 `data/xianyu_data.db`，实际是指向受保护状态区的符号链接。加密密钥同样存放在状态区。源码目录被清理时，状态区和恢复包不会随之消失。

## 手动创建与验证恢复包

```bash
cd /Users/w/PycharmProjects/xianyu-auto-reply
archive="$(./scripts/create_recovery_backup.sh)"
./scripts/verify_recovery_backup.sh "$archive"
```

恢复包内包含项目源码、SQLite 一致性快照、个人 IP 素材与内容商品包。`venv`、运行日志和浏览器缓存会被排除，以减小体积并避免保存短期缓存。

## Git 工作流

网页热更新的写入和删除入口默认锁定。源码更新统一使用 Git：

```bash
git fetch origin
git status
git pull --ff-only
```

要保留本地修改时，先新建 `codex/` 或 `feature/` 分支并提交，再推送远端。业务数据库、Cookie、API Key、个人素材和恢复包均由 `.gitignore` 排除。

## 恢复顺序

1. 从 GitHub 拉取最后一个已验证分支。
2. 从最新恢复包解压 `project/data` 和本地素材。
3. 运行 `scripts/install_protected_state.sh`，把数据库和密钥装入项目外状态区。
4. 新建虚拟环境并安装 `requirements.txt`。
5. 运行 `./start.sh`，确认首页、管理后台和完整性看守均正常。

首次恢复后，如果历史 Cookie 或 API Key 的原加密密钥已遗失，需要在管理后台重新保存一次对应值。此后的新密钥会同时保存在状态区和加密恢复包中。
