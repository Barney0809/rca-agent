# 被诊断系统的镜像。三个服务共用同一个镜像，靠 compose 里的 command 区分角色。
#
# 为什么共用镜像：三个服务的代码结构、依赖完全相同，只有启动模块不同。
# 建三个镜像只会让构建变慢、体积变大，没有收益。

FROM python:3.14-slim

# 让 Python 的日志立刻刷到 stdout，不要缓冲。
# 不加这行的话，容器日志会攒一批才输出，实时观测就废了。
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 先装依赖再拷代码：这样只要依赖没变，改代码不会触发重新安装（利用构建缓存）
COPY world/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 拷贝被诊断系统代码
COPY world/ /app/world/

# 让 `python -m uvicorn world.order.main:app` 能找到 world 包
ENV PYTHONPATH=/app

# 默认启动 order；compose 里会按服务覆盖 command
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "world.order.main:app", "--host", "0.0.0.0", "--port", "8000"]
