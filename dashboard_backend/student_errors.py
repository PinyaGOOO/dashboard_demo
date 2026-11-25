"""Errors safe to expose to the student or teacher web client."""


class StudentError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
