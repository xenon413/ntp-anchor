class NTPAnchorError(Exception):
    '''Base exception for all errors raised by the ntp-anchor package.'''
    pass

class TimeSyncPermissionError(NTPAnchorError):
    '''Raised when the application lacks Admin/Sudo privileges to sync the clock.'''
    pass

class UnsupportedOSError(NTPAnchorError):
    '''Raised when the package is executed on an unsupported operating system.'''
    pass

class UnexpectedError(NTPAnchorError):
    '''Raised when an unexpected error occurs.'''
    pass
