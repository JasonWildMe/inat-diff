"""Custom exceptions for iNaturalist API interactions."""


class iNatAPIError(Exception):
    """Base exception for iNaturalist API errors.

    status_code / retry_after are set when the failure was an HTTP response.
    """

    def __init__(self, message: str = "", status_code=None, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class PlaceNotFoundError(iNatAPIError):
    """Raised when a requested place/region cannot be found."""
    pass


class TaxonNotFoundError(iNatAPIError):
    """Raised when a requested taxon cannot be found."""
    pass


class InvalidTimeFormatError(iNatAPIError):
    """Raised when time period format is invalid."""
    pass