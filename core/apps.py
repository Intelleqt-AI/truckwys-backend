from django.apps import AppConfig

class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        """Import signals when app is ready."""
        # Fail closed at boot (web, worker, beat, manage.py) rather than at the
        # first Xero/Cartrack call: production must have FIELD_ENCRYPTION_KEY.
        from core.utils.crypto import validate_encryption_config
        validate_encryption_config()
        import core.signals  # noqa
        import core.accounting.signals  # noqa
        from core.ws import data_changes
        data_changes.connect()
        from core.services import mail_delivery
        mail_delivery.install()
