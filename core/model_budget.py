from contextvars import ContextVar


MODEL_EVENT = ContextVar('travel_model_event', default='')


class ModelBudgetExceeded(ValueError):
    pass
