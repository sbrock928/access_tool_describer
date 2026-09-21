# Access Extraction

The Windows adapter is deliberately conservative. It validates the staged artifact, creates a hidden Access automation instance, requests forced macro security, and obtains schema/query metadata without executing QueryDefs. It never calls `DoCmd.OpenForm`, `DoCmd.OpenReport`, `Run`, `RunCode`, macro execution, or query execution. Forms, reports, macros, modules, and queries are exported with `Application.SaveAsText` for static parsing where supported.

Microsoft documents `SaveAsText` as exporting object definitions and `AutomationSecurity` as controlling programmatic-open security. These controls reduce risk but do not create a guarantee across every Access version, trust-center setting, encrypted database, or malicious file. Run the adapter only in an isolated Windows analysis environment, with a non-privileged account and no production credentials. See the Microsoft references in `WINDOWS_SETUP.md`.
