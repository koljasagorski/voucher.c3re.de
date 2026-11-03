"""gunicorn logger that masks tokens (magic links, action links) in access log lines."""

from gunicorn.glogging import Logger

from voucher.logging import mask_codes


def mask_atoms(atoms):
    return {key: mask_codes(value) if isinstance(value, str) else value for key, value in atoms.items()}


class MaskingLogger(Logger):
    def atoms(self, resp, req, environ, request_time):
        return mask_atoms(super().atoms(resp, req, environ, request_time))
