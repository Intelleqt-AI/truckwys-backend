from django.apps import AppConfig

class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        """Import signals when app is ready."""
        import core.signals  # noqa
        from core.ws import data_changes
        data_changes.connect()
        from core.services import mail_delivery
        mail_delivery.install()
