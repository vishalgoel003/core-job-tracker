#!/bin/bash
echo "Starting background scheduler..."
python src/scheduler.py &

echo "Starting Streamlit..."
exec streamlit run web/app.py --server.port=8501 --server.address=0.0.0.0
