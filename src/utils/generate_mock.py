import json
import os
from pathlib import Path

def generate_mock_data():
    # 2,000-word placeholder text
    placeholder_word = "lorem "
    full_text = (placeholder_word * 2000).strip()
    
    mock_data = {
        "title": "Efficacy of Novel Therapeutics in Canine Parvovirus Infection",
        "journal": "JVIM",
        "abstract": "This study evaluates the efficacy of a novel therapeutic approach for treating Canine Parvovirus.",
        "species": "Canine",
        "full_text": full_text
    }
    
    # Ensure the output directory exists
    output_dir = Path("tests/fixtures")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    output_file = output_dir / "sample_paper.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(mock_data, f, indent=4)
        
    print(f"Mock data generated at {output_file}")

if __name__ == "__main__":
    generate_mock_data()
