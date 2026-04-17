import uvicorn

if __name__ == "__main__":
    print("Starting Predictive Maintenance Agent...")
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
