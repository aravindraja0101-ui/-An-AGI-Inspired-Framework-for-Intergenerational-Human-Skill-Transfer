import time
import os
# pyrefly: ignore [parse-error]
from pipeline import ExperienceGPTPipeline

def test_pipeline():
    excel_path = "medical_experience_transfer_dataset.xlsx"
    cache_path = "temporary_search_db.pkl"
    
    print("=== Testing Experience Agents Core Engine ===")
    
    # 1. Initialization
    start_time = time.time()
    # pyrefly: ignore [parse-error]
    pipeline = ExperienceGPTPipeline(excel_path=excel_path, cache_path=cache_path)
    init_duration = time.time() - start_time
    print(f"Pipeline initialized in {init_duration:.2f} seconds.")
    
    # Verify temporary dataframe columns
    expected_cols = ['Case_ID', 'Department', 'Chief_Complaint', 'Symptoms', 
                     'Medical_History', 'Diagnosis', 'Disease_Stage', 'Comorbidities',
                     'Search_Document', 'Embedding']
    
    for col in expected_cols:
        assert col in pipeline.temporary_search_df.columns, f"Missing column: {col}"
    print("temporary_search_df schema validated.")
    
    # 2. Text Normalization Test
    raw_text = "Patient presents with sudden onset RIGHT-SIDED weakness, difficulty speaking... history of diabetes."
    cleaned = pipeline.clean_text(raw_text)
    print(f"\nRaw text: '{raw_text}'")
    print(f"Cleaned text: '{cleaned}'")
    assert "right sided" in cleaned, "Punctuation removal failed"
    assert "RIGHT-SIDED" not in cleaned, "Case conversion failed"
    print("Text cleaning logic validated.")
    
    # 3. Keyword Extraction Test
    sample_query = "A 62 year old male presents with sudden onset right-sided weakness, difficulty speaking, facial asymmetry. History of hypertension and diabetes."
    print(f"\nExtracting keywords from query: '{sample_query}'")
    kws = pipeline.extract_keywords(sample_query)
    print("Extracted Keywords:")
    print(" - Departments:", kws['departments'])
    print(" - Diagnoses:", kws['diagnoses'])
    print(" - Symptoms:", kws['symptoms'])
    print(" - Histories:", kws['histories'])
    
    # Check if we got the expected key terms
    assert any("weakness" in s for s in kws['symptoms']) or any("asymmetry" in s for s in kws['symptoms']), "Symptoms not extracted"
    assert any("diabetes" in h or "hypertension" in h for h in kws['histories']), "Medical history not extracted"
    print("Keyword extraction validated.")
    
    # 4. Candidate Filtering Test
    filtered_df, filter_msg = pipeline.filter_candidates(kws)
    print(f"\nFilter Message: {filter_msg}")
    print(f"Filtered Candidates: {len(filtered_df)} rows.")
    assert len(filtered_df) > 0, "Filtering resulted in empty candidates"
    assert len(filtered_df) <= len(pipeline.temporary_search_df), "Filtered candidates larger than original"
    print("Candidate filtering validated.")
    
    # 5. Semantic Search Test
    print("\nRunning Semantic Search for top 5 matches...")
    search_results = pipeline.search(sample_query, k=5)
    print(f"Found {len(search_results['results'])} matches:")
    for res in search_results['results']:
        print(f" Rank {res['rank']}: {res['Case_ID']} - Similarity: {res['Similarity_Pct']:.2f}% | Diagnosis: {res['Diagnosis']} | Dept: {res['Department']}")
        
    print("\n=== All Core Engine Tests Passed! ===")

if __name__ == "__main__":
    test_pipeline()
