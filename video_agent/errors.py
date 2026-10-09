class VideoAgentError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str | None = None,
        retryable: bool = False,
        task_id: str | None = None,
        generation_id: str | None = None,
        plan_version: int | None = None,
        fields: list[str] | None = None,
        shot_ids: list[str] | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.retryable = retryable
        self.task_id = task_id
        self.generation_id = generation_id
        self.plan_version = plan_version
        self.fields = fields
        self.shot_ids = shot_ids
        self.details = details
