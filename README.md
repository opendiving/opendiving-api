# Open Diving API

Backend API for Open Diving app.

## Endpoints

### Parse a Suunto dive XML file

Upload a Suunto dive XML file and receive the parsed dive data as JSON:

```bash
curl -X POST http://localhost:8000/api/v1/dive/parse-xml \
  -F "file=@Dive_2021-04-06-1231.xml"
```
