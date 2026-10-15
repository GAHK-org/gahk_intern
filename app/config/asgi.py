"""
ASGI config for config project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
# Serve from a connection pool, not persistent connections, which ASGI leaks — see DATABASES in
# config/settings.py. Set here because this module is exactly what runs under ASGI.
os.environ.setdefault("DATABASE_POOL", "1")

application = get_asgi_application()
