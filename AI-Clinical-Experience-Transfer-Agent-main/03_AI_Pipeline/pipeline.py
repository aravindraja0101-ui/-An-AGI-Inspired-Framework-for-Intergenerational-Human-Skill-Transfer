import os
import re
import string
import pickle
import pandas as pd
import numpy as np
from sentence_transformers import SentenceTransformer
import faiss
import nltk

# Ensure NLTK resources are available
try:
    nltk.data.find('tokenizers/punkt')
except LookupError:
    nltk.download('punkt', quiet=True)

class ExperienceGPTPipeline:
    def __init__(self, excel_path, cache_path="temporary_search_db.pkl"):
        self.excel_path = excel_path
        self.cache_path = cache_path
        
        self.df_original = None
        self.temporary_search_df = None
        self.faiss_index = None
        self.case_id_to_index = {}
        self.index_to_case_id = {}
        
        # Load the sentence transformer model
        print("Loading SentenceTransformer model...")
        self.model = SentenceTransformer('all-MiniLM-L6-v2')
        
        # Load data and prepare index
        self.initialize_pipeline()
        
        # Build the medical vocabulary for keyword extraction
        self.build_medical_vocabulary()

    def clean_text(self, text):
        """
        Step 2: Normalize the text.
        - convert to lowercase
        - remove punctuation (replace with space to avoid merging words)
        - remove duplicate spaces
        - remove leading/trailing spaces
        - Do NOT remove medical terminology.
        """
        if pd.isna(text):
            return ""
        
        text = str(text).lower()
        # Replace punctuation with a space to prevent word merging (e.g. right-sided -> right sided)
        # Using string.punctuation
        punct_translator = str.maketrans(string.punctuation, ' ' * len(string.punctuation))
        text = text.translate(punct_translator)
        
        # Remove duplicate spaces and strip
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    def initialize_pipeline(self):
        """
        Loads the original dataset and creates/loads the temporary search database.
        """
        print("Loading original dataset...")
        self.df_original = pd.read_excel(self.excel_path)
        
        # Check if cache exists
        if os.path.exists(self.cache_path):
            print(f"Loading cached search database from {self.cache_path}...")
            with open(self.cache_path, 'rb') as f:
                cache_data = pickle.load(f)
                self.temporary_search_df = cache_data['df']
                self.case_id_to_index = cache_data['case_id_to_index']
                self.index_to_case_id = cache_data['index_to_case_id']
                
            # Rebuild FAISS Index from cached embeddings
            embeddings = np.array(self.temporary_search_df['Embedding'].tolist()).astype('float32')
            dimension = embeddings.shape[1]
            self.faiss_index = faiss.IndexFlatL2(dimension)
            self.faiss_index.add(embeddings)
            print("FAISS index rebuilt from cache.")
        else:
            print("Generating temporary search database...")
            # Step 1: Create a new dataframe named temporary_search_df containing selected columns
            search_cols = [
                'Case_ID', 'Department', 'Chief_Complaint', 'Symptoms', 
                'Medical_History', 'Diagnosis', 'Disease_Stage', 'Comorbidities'
            ]
            self.temporary_search_df = self.df_original[search_cols].copy()
            
            # Step 2: Normalize the text for every text column
            # Except Case_ID, clean all columns
            for col in search_cols:
                if col != 'Case_ID':
                    self.temporary_search_df[col] = self.temporary_search_df[col].apply(self.clean_text)
                    
            # Step 3: Generate Search Document by concatenating fields
            def create_search_document(row):
                parts = [
                    str(row['Department']),
                    str(row['Chief_Complaint']),
                    str(row['Symptoms']),
                    str(row['Medical_History']),
                    str(row['Diagnosis']),
                    str(row['Disease_Stage']),
                    str(row['Comorbidities'])
                ]
                # Join with newlines (or double newlines for separation)
                return "\n\n".join([p for p in parts if p])
            
            self.temporary_search_df['Search_Document'] = self.temporary_search_df.apply(create_search_document, axis=1)
            
            # Step 4: Generate Embeddings
            print("Generating embeddings for search documents (this may take a minute)...")
            docs = self.temporary_search_df['Search_Document'].tolist()
            # Normalize embeddings to unit L2 length for exact cosine similarity mapping
            embeddings_list = self.model.encode(docs, show_progress_bar=True, normalize_embeddings=True)
            self.temporary_search_df['Embedding'] = list(embeddings_list)
            
            # Step 5: Build FAISS Index
            embeddings_np = np.array(embeddings_list).astype('float32')
            dimension = embeddings_np.shape[1]
            self.faiss_index = faiss.IndexFlatL2(dimension)
            self.faiss_index.add(embeddings_np)
            
            # Maintain case_id_to_index mapping
            for idx, row in self.temporary_search_df.iterrows():
                cid = row['Case_ID']
                self.case_id_to_index[cid] = idx
                self.index_to_case_id[idx] = cid
                
            # Cache the database
            print(f"Caching search database to {self.cache_path}...")
            with open(self.cache_path, 'wb') as f:
                pickle.dump({
                    'df': self.temporary_search_df,
                    'case_id_to_index': self.case_id_to_index,
                    'index_to_case_id': self.index_to_case_id
                }, f)
            print("Caching complete.")

    def build_medical_vocabulary(self):
        """
        Compiles a vocabulary of known symptoms, histories, diagnoses, and departments
        directly from the original dataset for robust local keyword extraction.
        """
        print("Building medical vocabulary...")
        
        # Helper to split, clean and filter entries
        def extract_terms(series, split_char=None):
            terms = set()
            for val in series.dropna():
                val_str = str(val).strip()
                if not val_str:
                    continue
                if split_char:
                    parts = re.split(split_char, val_str)
                else:
                    parts = [val_str]
                for p in parts:
                    clean_p = self.clean_text(p)
                    # Ignore short words or empty
                    if len(clean_p) > 2 and clean_p not in ["no comorbidities reported", "no comorbidities", "none"]:
                        terms.add(clean_p)
            return sorted(list(terms), key=len, reverse=True)

        self.vocab_departments = extract_terms(self.df_original['Department'])
        self.vocab_diagnoses = extract_terms(self.df_original['Diagnosis'])
        # Symptoms are comma separated
        self.vocab_symptoms = extract_terms(self.df_original['Symptoms'], split_char=r',|;')
        # Medical history can be separated by 'and', 'or', commas
        self.vocab_histories = extract_terms(self.df_original['Medical_History'], split_char=r',|;|\band\b|\bor\b')

    def extract_keywords(self, query):
        """
        Step 7: Dynamic Medical Keyword Extraction.
        Extracts medical keywords from a free-form query based on our dataset vocabulary.
        Uses greedy phrase matching (longer phrases first) to extract keywords.
        """
        normalized_query = self.clean_text(query)
        
        extracted = {
            'departments': [],
            'diagnoses': [],
            'symptoms': [],
            'histories': []
        }
        
        temp_query = f" {normalized_query} " # Add padding for word boundary checking
        
        # Helper to find matches using word boundary regex
        def match_vocab(vocab_list, category):
            nonlocal temp_query
            for term in vocab_list:
                # Create a regex to match the exact term with word boundaries
                pattern = r'\b' + re.escape(term) + r'\b'
                if re.search(pattern, temp_query):
                    extracted[category].append(term)
                    # Remove matched term from temp_query to avoid double matching shorter sub-phrases
                    temp_query = re.sub(pattern, ' ', temp_query)
                    # Clean double spaces
                    temp_query = re.sub(r'\s+', ' ', temp_query)

        # Match departments, diagnoses, symptoms, and histories
        match_vocab(self.vocab_departments, 'departments')
        match_vocab(self.vocab_diagnoses, 'diagnoses')
        match_vocab(self.vocab_symptoms, 'symptoms')
        match_vocab(self.vocab_histories, 'histories')
        
        return extracted

    def filter_candidates(self, extracted_keywords):
        """
        Step 8: Candidate Filtering.
        Filters temporary_search_df using the extracted medical keywords.
        Keep cases that match at least one extracted keyword in Department, Diagnosis, Symptoms, or Medical History.
        If no keywords are extracted, or no cases match, returns the full dataframe.
        """
        has_keywords = any(len(kws) > 0 for kws in extracted_keywords.values())
        if not has_keywords:
            return self.temporary_search_df.copy(), "No keywords extracted. Defaulting to all cases."
        
        matching_indices = []
        
        for idx, row in self.temporary_search_df.iterrows():
            match = False
            
            # 1. Check Department match
            for kw in extracted_keywords['departments']:
                if kw in row['Department']:
                    match = True
                    break
            
            # 2. Check Diagnosis match
            if not match:
                for kw in extracted_keywords['diagnoses']:
                    if kw in row['Diagnosis']:
                        match = True
                        break
            
            # 3. Check Symptoms or Chief Complaint match
            if not match:
                for kw in extracted_keywords['symptoms']:
                    if kw in row['Symptoms'] or kw in row['Chief_Complaint']:
                        match = True
                        break
            
            # 4. Check Medical History or Comorbidities match
            if not match:
                for kw in extracted_keywords['histories']:
                    if kw in row['Medical_History'] or kw in row['Comorbidities']:
                        match = True
                        break
                        
            if match:
                matching_indices.append(idx)
                
        if len(matching_indices) == 0:
            return self.temporary_search_df.copy(), "No database cases matched the extracted keywords exactly. Defaulting to all cases."
            
        filtered_df = self.temporary_search_df.loc[matching_indices].copy()
        msg = f"Filtered search database from {len(self.temporary_search_df)} down to {len(filtered_df)} candidate cases."
        return filtered_df, msg

    def search(self, query, k=10):
        """
        Runs the full search pipeline:
        1. Extract keywords
        2. Filter candidates
        3. Semantic search among filtered candidates
        4. Retrieve original records
        """
        # Step 7: Extract medical keywords
        extracted_kws = self.extract_keywords(query)
        
        # Step 8: Filter candidates
        filtered_df, filter_msg = self.filter_candidates(extracted_kws)
        
        # Step 9: Semantic Search
        # Embed the query
        query_cleaned = self.clean_text(query)
        query_embedding = self.model.encode([query_cleaned], normalize_embeddings=True)[0]
        
        # Retrieve embeddings of candidate cases
        candidate_embeddings = np.array(filtered_df['Embedding'].tolist()).astype('float32')
        candidate_indices = filtered_df.index.tolist()
        
        # Calculate distances (since embeddings are L2 normalized, cosine similarity is 1 - distance_squared / 2)
        # We calculate squared L2 distances
        diffs = candidate_embeddings - query_embedding
        squared_distances = np.sum(diffs ** 2, axis=1)
        
        # Sort and select top K
        sorted_indices = np.argsort(squared_distances)
        top_k_indices = sorted_indices[:k]
        
        results = []
        for rank, s_idx in enumerate(top_k_indices):
            orig_df_idx = candidate_indices[s_idx]
            dist_sq = squared_distances[s_idx]
            
            # Cosine similarity mapped to %
            # If embeddings are normalized, L2 distance squared is in [0, 4]
            # dist_sq = 2 - 2 * cos_sim => cos_sim = 1 - dist_sq / 2
            cosine_similarity = 1.0 - (dist_sq / 2.0)
            similarity_pct = max(0.0, min(100.0, cosine_similarity * 100.0))
            
            # Get case details
            case_id = filtered_df.loc[orig_df_idx, 'Case_ID']
            
            results.append({
                'rank': rank + 1,
                'df_index': orig_df_idx,
                'Case_ID': case_id,
                'Similarity_Pct': similarity_pct,
                'Distance': float(dist_sq)
            })
            
        # Step 10: Retrieve original rows using Case_ID
        final_results = []
        for res in results:
            case_id = res['Case_ID']
            original_row = self.df_original[self.df_original['Case_ID'] == case_id].iloc[0].to_dict()
            
            # Combine search result metadata with original record
            merged = {**res, **original_row}
            final_results.append(merged)
            
        return {
            'query': query,
            'extracted_keywords': extracted_kws,
            'filter_message': filter_msg,
            'candidate_count': len(filtered_df),
            'results': final_results
        }
