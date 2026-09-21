# Windows Setup

Use 64-bit Python and the same Office/Access bitness. Install Python 3.13, Microsoft Access, and `pywin32`, then run `pip install -e ".[dev,windows]"`. Run Access integration tests only on an isolated workstation with a synthetic database:

```powershell
pytest -m windows_access
```

The analyzer does not authenticate to external data sources. Use an account without production access and ensure the staging workspace is local and writable.

References: [AutomationSecurity](https://learn.microsoft.com/en-us/office/vba/api/Access.Application.AutomationSecurity), [Access startup/AutoExec behavior](https://support.microsoft.com/en-us/access/create-a-macro-that-runs-when-you-open-a-database), [OpenCurrentDatabase](https://learn.microsoft.com/en-us/office/vba/api/access.application.opencurrentdatabase), and [SaveAsText](https://learn.microsoft.com/en-us/office/client-developer/access/desktop-database-reference/application-save-as-text).
