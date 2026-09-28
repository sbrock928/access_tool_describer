# Windows Setup

Use Python 3.13 and Microsoft Access/ACE with matching bitness. Install the Windows and development
extras:

```powershell
pip install -e ".[dev,windows]"
```

Run extraction under a low-privilege identity on an isolated workstation or worker with no
production credentials. Before enabling SaveAsText export, the launcher must attest that a non-low
Access macro policy and outbound-network blocking are active; the runtime independently rejects an
administrator token. Otherwise the mandatory DAO lane still runs and export coverage is marked
partial.

Run the synthetic integration suite only in that isolated environment:

```powershell
pytest -m windows_access
```

The integration boundary must demonstrate no AutoExec/startup marker, DNS/TCP activity, credential
prompt, orphan Access process, stuck Shift key, unsafe export path, or raw export leak.

Microsoft references: [AutomationSecurity](https://learn.microsoft.com/en-us/office/vba/api/Access.Application.AutomationSecurity),
[AllowBypassKey](https://support.microsoft.com/en-us/access/allowbypasskey-property), and
[QueryDef.Connect](https://learn.microsoft.com/en-us/office/client-developer/access/desktop-database-reference/querydef-connect-property-dao).
