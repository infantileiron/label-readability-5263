"""测试环境引导：在导入应用模块前提供占位环境变量。

无论以 `python -m unittest discover -s tests`（测试文件作为顶层模块）
还是包路径方式运行都先执行本模块，保证 settings 能完成导入。
真实环境变量优先，不被覆盖。
"""
import os

os.environ.setdefault('DATABASE_URL', 'postgresql://proof:test@localhost:5432/proof')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')
os.environ.setdefault('JWT_SECRET', 'test-secret-not-for-production')
