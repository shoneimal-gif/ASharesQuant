# 在 Codex 云端继续开发和验证

这两个文件已经准备好：`CLOUD_TASK.txt` 是接续任务，本文是进入云端的步骤。**当前文件的准备不代表任务已经在云端运行。**

## 1. 把源码放入云端能读取的位置

将本目录的源码、配置和说明书放入你选择的 GitHub 仓库，并让该仓库关联到 Codex 云端环境。可以使用已有仓库；仓库名称和环境由你实际选择。若你现有环境明确支持上传代码，也可上传完整源码目录或压缩包，并确认环境已解压、能读取 `main.py`。只贴任务文字不能替代源码导入。

本次离线验证不需要任何行情密钥。源码中不要放 `.env`、账户文件或真实 Token。

## 2. 选择云端环境

在网页或桌面应用中打开 **Work in → Cloud → Select environment**。选择已含源码的环境；没有合适环境时选择 **Create environment**，连接并选择实际 GitHub 仓库，完成设置后选择 **Publish**，等到 **Environment published** 再开始任务。这是 [OpenAI 官方 Codex Cloud 指南](https://learn.chatgpt.com/docs/cloud)给出的入口和流程。

环境需要可用的 Python 3.10+。本项目使用标准库，不需要安装 pip 包，也不需要为离线验收开启行情网站访问。

## 3. 提交接续任务

在所选环境开始一个任务，把同目录 `CLOUD_TASK.txt` 的全部内容粘贴进去。要求先确认实际源码目录，再执行开发和验证。

云端应运行标准库测试、合成演示与 CSV 模式，交付 `CLOUD_VALIDATION_REPORT.md` 和 `CLOUD_VALIDATION_SUMMARY.json`。查看实际命令、退出码、失败/跳过项与修改内容，再决定后续真实数据接入。`run_ashare.cmd` 是本机 Windows 入口；云端使用环境中可用的 Python 直接运行 `main.py`。

这一步用于工程验证。真实 A 股数据源仍未选择，合成结果不能证明策略盈利；长期行情运行和部署另行安排。
