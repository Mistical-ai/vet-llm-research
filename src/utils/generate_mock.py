"""
Mock Data Generator for Veterinary Papers.

Design Decisions:
- Why we use JSON for mock data: JSON (JavaScript Object Notation) is a standard format 
  that organizes data like a dictionary (keys and values). It is incredibly easy for both 
  humans to read and computers (like our AI models) to process.
- Why we generate synthetic text: We need a safe, consistent, and free way to test our 
  pipeline before feeding it real, potentially copyrighted or sensitive veterinary research papers.
"""
import json
import os
from pathlib import Path

def generate_mock_data():
    """
    Creates a fake veterinary research paper and saves it as a JSON file.
    """
    # Define the basic information (metadata) about our fake research paper
    title = "Evaluation of Serum Pancreatic Lipase Immunoreactivity in Dogs with Acute Abdominal Syndrome"
    journal = "Journal of Veterinary Internal Medicine (JVIM)"
    species = "Canine"

    # Write the abstract, which is a short summary of the study
    abstract = (
        "Objective: To evaluate the diagnostic utility of serum canine pancreatic lipase immunoreactivity (cPLI) "
        "in dogs presenting with acute abdominal syndrome. "
        "Methods: In this prospective observational study, 150 client-owned dogs presenting with acute abdominal pain, "
        "vomiting, and lethargy were enrolled at a veterinary teaching hospital. Serum cPLI concentrations were measured "
        "at admission using a validated commercially available assay. Dogs were categorized into three groups based on "
        "comprehensive diagnostic imaging, cytology, and final clinical diagnosis: acute pancreatitis (AP), non-pancreatic "
        "acute abdomen (NPAA), and healthy controls. "
        "Results: Serum cPLI concentrations were significantly higher in dogs with AP (median 850 µg/L, range 400-2000 µg/L) "
        "compared to dogs with NPAA (median 120 µg/L, range 20-350 µg/L) and healthy controls (median 30 µg/L, range 10-80 µg/L) "
        "(p < 0.001). A cut-off value of 400 µg/L yielded a sensitivity of 92% and a specificity of 88% for the diagnosis of AP. "
        "Clinical signs such as vomiting and cranial abdominal pain were most strongly associated with cPLI elevations. "
        "Conclusions: Serum cPLI is a highly sensitive and specific biomarker for diagnosing acute pancreatitis in dogs "
        "presenting with acute abdominal syndrome. Its routine use in emergency settings can facilitate early and accurate "
        "diagnosis, allowing for prompt initiation of targeted medical management and potentially improving clinical outcomes."
    )

    # Write the different sections of the full research paper
    intro = (
        "Introduction\n\n"
        "Acute abdominal syndrome in dogs represents a common and challenging clinical presentation in veterinary emergency medicine. "
        "The differential diagnosis is extensive, ranging from benign gastrointestinal upset to life-threatening conditions such as "
        "gastric dilatation-volvulus, septic peritonitis, and acute pancreatitis (AP). The burden of pancreatitis in dogs is substantial, "
        "often leading to severe morbidity and mortality if not recognized and managed promptly. The clinical presentation of AP is highly "
        "variable, with clinical signs including vomiting, anorexia, lethargy, and cranial abdominal pain. Traditional diagnostic markers, "
        "such as serum amylase and lipase activities, lack adequate sensitivity and specificity for the diagnosis of canine acute pancreatitis, "
        "as these enzymes can originate from non-pancreatic sources and are affected by renal clearance. Consequently, the development and "
        "validation of pancreas-specific biomarkers have been a major focus of veterinary gastroenterology. Canine pancreatic lipase "
        "immunoreactivity (cPLI) has emerged as a promising diagnostic tool. Unlike catalytic assays, cPLI measures the mass of the enzyme "
        "and is highly specific to the exocrine pancreas. Historically, the diagnosis of AP relied heavily on clinical suspicion combined "
        "with ultrasonography, which is highly operator-dependent. The introduction of cPLI has revolutionized the non-invasive diagnosis "
        "of this condition. The pathophysiology of acute pancreatitis involves the premature activation of digestive zymogens within the "
        "pancreatic acinar cells, leading to autodigestion, severe local inflammation, and the release of inflammatory mediators into the "
        "systemic circulation. This systemic inflammatory response syndrome (SIRS) can progress to multiple organ dysfunction syndrome (MODS), "
        "which is the primary cause of mortality in severe cases. Given the rapid progression and potential severity of the disease, early "
        "and accurate diagnosis is paramount. The clinical signs, however, are notoriously non-specific. Vomiting and cranial abdominal pain "
        "are frequently observed but are also common in numerous other gastrointestinal and systemic diseases. This overlap in clinical "
        "presentation underscores the critical need for a diagnostic test that can reliably differentiate acute pancreatitis from other "
        "causes of acute abdomen. While previous studies have evaluated cPLI in experimental models and specific clinical subsets, "
        "comprehensive data regarding its utility in a generalized population of dogs presenting with acute abdominal syndrome remain limited. "
        "Furthermore, the correlation between cPLI concentrations and specific clinical parameters, such as the severity of cranial abdominal "
        "pain, requires further elucidation. Therefore, the objective of this study was to evaluate the diagnostic performance of serum cPLI "
        "in a large cohort of dogs presenting with acute abdomen, and to determine its association with specific clinical signs and outcomes.\n\n"
    )

    methods = (
        "Methods\n\n"
        "This was a prospective observational study conducted at a tertiary veterinary referral center over a 24-month period. "
        "Client-owned dogs presenting to the emergency service with clinical signs of acute abdomen, specifically vomiting, anorexia, "
        "and cranial abdominal pain, were evaluated for inclusion. Dogs were excluded if they had a history of chronic exocrine pancreatic "
        "insufficiency, had received corticosteroid therapy within the previous 14 days, or if a complete diagnostic workup could not be "
        "performed. Upon admission, a standardized clinical assessment was conducted, and the severity of clinical signs was graded using "
        "a modified clinical severity index. Blood samples were collected via jugular venipuncture prior to the administration of any "
        "medical therapy, including intravenous fluids or analgesics. Serum was separated within 30 minutes of collection and stored at "
        "-80°C until batch analysis. Serum cPLI concentrations were measured using a validated, commercially available enzyme-linked "
        "immunosorbent assay (Spec cPL, IDEXX Laboratories). All dogs underwent a comprehensive diagnostic evaluation, which included a "
        "complete blood count (CBC), serum biochemical profile, urinalysis, and abdominal ultrasonography. Abdominal radiographs were "
        "performed at the discretion of the attending clinician. The abdominal ultrasonography was performed by a board-certified veterinary "
        "radiologist using a high-resolution ultrasound machine equipped with a broadband linear array transducer. The pancreas was "
        "systematically evaluated for size, echogenicity, margin definition, and the presence of peripancreatic fluid or fat saponification. "
        "The sonographic diagnosis of acute pancreatitis was based on the presence of an enlarged, hypoechoic pancreas with surrounding "
        "hyperechoic mesenteric fat. The clinical severity index used in this study incorporated parameters such as heart rate, respiratory "
        "rate, body temperature, degree of dehydration, and the presence of systemic inflammatory response syndrome (SIRS) criteria. "
        "The final clinical diagnosis was established by a panel of three board-certified veterinary internists who were blinded to the "
        "cPLI results. The diagnosis of AP was based on a combination of compatible clinical signs, characteristic ultrasonographic findings, "
        "and exclusion of other causes of acute abdomen. Statistical analysis was performed using commercially available software. "
        "Continuous variables were assessed for normality using the Shapiro-Wilk test. Given the non-normal distribution of the biomarker "
        "data, non-parametric tests were employed. The Kruskal-Wallis statistical analysis was utilized to compare median cPLI concentrations "
        "across the different diagnostic groups (AP, non-pancreatic acute abdomen, and healthy controls). Post-hoc pairwise comparisons "
        "were performed using the Dunn's test with Bonferroni correction to adjust for multiple comparisons. Categorical variables, such as "
        "the presence or absence of specific clinical signs, were compared using the Chi-square test or Fisher's exact test as appropriate. "
        "Correlation between continuous variables was assessed using Spearman's rank correlation coefficient. A p-value of p < 0.05 was "
        "considered statistically significant for all analyses.\n\n"
    )

    results = (
        "Results\n\n"
        "A total of 150 dogs met the inclusion criteria and were enrolled in the study. The study population consisted of various breeds, "
        "with Miniature Schnauzers, Yorkshire Terriers, and mixed breed dogs being overrepresented. The median age was 7.5 years (range, "
        "1 to 15 years), and there was an equal distribution of males and females. Based on the comprehensive diagnostic evaluation, 50 "
        "dogs were diagnosed with acute pancreatitis (AP), 75 dogs were diagnosed with non-pancreatic acute abdomen (NPAA), and 25 "
        "clinically healthy dogs were included as controls. The NPAA group included dogs with acute gastroenteritis (n=30), gastrointestinal "
        "foreign body obstruction (n=20), hemorrhagic gastroenteritis (n=15), and other causes (n=10). The most frequently observed clinical "
        "signs in the AP group included vomiting (92%), anorexia (88%), and cranial abdominal pain (76%). Regarding the complete blood count "
        "and serum biochemical profile, leukocytosis with a left shift was observed in 65% of dogs with AP, compared to 40% of dogs with NPAA. "
        "Mild to moderate elevations in liver enzymes, specifically alanine aminotransferase (ALT) and alkaline phosphatase (ALP), were common "
        "in both groups but were significantly higher in the AP group (p < 0.05). Hyperbilirubinemia was noted in 15% of the AP cases, likely "
        "secondary to extrahepatic biliary tract obstruction from pancreatic inflammation. Abdominal ultrasonography identified abnormalities "
        "consistent with AP in 85% of the dogs ultimately diagnosed with the condition. The most common sonographic findings were an enlarged, "
        "hypoechoic pancreas (80%) and peripancreatic fat saponification (70%). Interestingly, 15% of dogs with a final diagnosis of AP had a "
        "normal initial abdominal ultrasound, highlighting the limitations of imaging in early or mild disease. Serum cPLI concentrations were "
        "significantly elevated in the AP group (median, 850 µg/L; interquartile range [IQR], 520-1200 µg/L) compared to the NPAA group "
        "(median, 120 µg/L; IQR, 45-210 µg/L) and healthy controls (median, 25 µg/L; IQR, 10-45 µg/L). The Kruskal-Wallis test revealed a "
        "highly significant difference among the groups (p < 0.001). Post-hoc analysis confirmed that cPLI levels in the AP group were "
        "significantly higher than both the NPAA group (p < 0.05) and controls (p < 0.05). Receiver operating characteristic (ROC) curve "
        "analysis demonstrated an area under the curve (AUC) of 0.94 for cPLI in distinguishing AP from NPAA. A cut-off value of 400 µg/L "
        "yielded a sensitivity of 88% and a specificity of 90%. Furthermore, within the AP group, dogs exhibiting severe cranial abdominal "
        "pain had significantly higher cPLI concentrations compared to those with mild or absent pain (p < 0.05). There was also a positive "
        "correlation between cPLI levels and the frequency of vomiting episodes during the first 24 hours of hospitalization (Spearman's rho = 0.65, p < 0.05).\n\n"
    )

    discussion = (
        "Discussion\n\n"
        "The findings of this prospective observational study strongly support the clinical utility of serum cPLI in the diagnostic workup "
        "of dogs presenting with acute abdominal syndrome. The significant elevation of cPLI in dogs with confirmed acute pancreatitis, "
        "compared to those with non-pancreatic causes of acute abdomen, highlights its value as a highly specific biomarker. The burden "
        "of pancreatitis in dogs necessitates rapid and accurate diagnostic tools to guide appropriate medical management, and our results "
        "indicate that cPLI fulfills this requirement. The sensitivity and specificity observed in our cohort align with previous studies, "
        "further validating the use of the 400 µg/L cut-off for diagnosing AP. The observation that 15% of our AP cases had normal initial "
        "ultrasonographic findings is particularly noteworthy. It reinforces the concept that ultrasonography, while highly specific when "
        "characteristic lesions are present, may lack sensitivity in the early stages of the disease or in cases of mild pancreatitis. "
        "In these instances, the marked elevation of serum cPLI provided the critical diagnostic clue that guided subsequent management. "
        "Interestingly, we observed a significant association between the magnitude of cPLI elevation and the severity of specific clinical "
        "signs, namely vomiting and cranial abdominal pain. This suggests that cPLI may not only serve as a diagnostic marker but could "
        "also reflect the severity of pancreatic inflammation and acinar cell damage. The positive correlation between cPLI concentrations "
        "and the severity of cranial abdominal pain and vomiting frequency is an intriguing finding that warrants further investigation. "
        "It is plausible that higher cPLI levels reflect a greater degree of acinar cell necrosis and subsequent release of pro-inflammatory "
        "cytokines, which in turn manifest as more severe clinical signs. If this relationship is confirmed in larger studies, cPLI could "
        "potentially be utilized not only as a diagnostic tool but also as a prognostic indicator to identify patients at higher risk for "
        "complications. The management of acute pancreatitis is primarily supportive, focusing on aggressive fluid therapy, analgesia, "
        "antiemetics, and early enteral nutrition. The prompt initiation of these therapies is crucial for optimizing patient outcomes. "
        "By facilitating an early and accurate diagnosis, cPLI empowers clinicians to implement targeted treatment strategies without delay. "
        "The economic burden of pancreatitis in dogs is also a significant consideration for pet owners. Prolonged hospitalization and "
        "intensive care can result in substantial financial costs. An accurate early diagnosis can help streamline the diagnostic workup, "
        "potentially reducing the need for unnecessary or repetitive testing, and allowing for more focused and cost-effective patient care. "
        "However, several limitations of this study must be acknowledged. The primary limitation is the relatively small sample size, "
        "particularly within the specific subgroups of non-pancreatic acute abdomen, which may limit the generalizability of the findings "
        "and the statistical power to detect smaller differences. Additionally, the study was conducted at a single tertiary referral center, "
        "potentially introducing selection bias towards more severe or complex cases. The lack of histopathological confirmation of pancreatitis "
        "in all cases is another inherent limitation of clinical studies in this field, as pancreatic biopsy is rarely justified in the "
        "acute clinical setting due to the associated risks of anesthesia and surgical intervention in unstable patients. Consequently, "
        "the diagnosis of AP relied on a combination of clinical, clinicopathologic, and ultrasonographic findings, which, while representing "
        "the current standard of care, may still result in some degree of misclassification. Future multi-center studies with larger cohorts "
        "are necessary to validate these results across diverse clinical settings and to further explore the utility of cPLI in differentiating "
        "mild from severe forms of AP. Furthermore, investigations into the prognostic value of serial cPLI monitoring during hospitalization "
        "and its correlation with clinical outcomes, length of hospital stay, and survival are warranted. In conclusion, serum cPLI is a robust "
        "and reliable biomarker for the diagnosis of acute pancreatitis in dogs with acute abdominal syndrome, providing critical information "
        "that can significantly impact patient management and care."
    )

    # Combine all the sections together to create the complete text of the paper
    full_text = intro + methods + results + discussion
    
    # We want our fake paper to be exactly 1500 words long. 
    # First, we split the text into a list of individual words by looking at the spaces.
    all_words_in_text = full_text.split()
    
    # Set our goal word count
    target_word_count = 1500
    
    # If our text is too short, we will add a generic sentence over and over until it's long enough
    if len(all_words_in_text) < 1450:
        padding_sentence = " Additional research is required to fully understand the long-term implications of these findings on canine health."
        while len(full_text.split()) < target_word_count:
            full_text += padding_sentence
    # If our text is too long, we will cut it down to exactly 1500 words
    elif len(all_words_in_text) > 1550:
        # Keep only the first 1500 words
        shortened_word_list = all_words_in_text[:target_word_count]
        
        # Glue the words back together with spaces in between
        full_text = " ".join(shortened_word_list)
        
        # Make sure the text ends with a period so it looks like a complete sentence
        if not full_text.endswith('.'):
            full_text += "..."
            
    # Count the final number of words to double-check our work
    final_word_count = len(full_text.split())

    # Package all our paper's information into a neat dictionary structure
    mock_paper_data = {
        "title": title,
        "journal": journal,
        "abstract": abstract,
        "species": species,
        "full_text": full_text
    }

    # Tell the computer where we want to save our new file
    output_folder_path = Path("tests/fixtures")
    
    # Create the folder if it doesn't exist yet (like making a new physical folder in a filing cabinet)
    output_folder_path.mkdir(parents=True, exist_ok=True)
    
    # Define the exact name and location for our new file
    output_file_path = output_folder_path / "sample_paper.json"
    
    # Open the file in "write" mode (represented by "w") and save our dictionary into it
    with open(output_file_path, "w", encoding="utf-8") as json_file_handle:
        # json.dump translates our Python dictionary into the standard JSON format and saves it
        json.dump(mock_paper_data, json_file_handle, indent=4)
        
    # Print a message to the screen so we know it worked
    print(f"Mock data generated at {output_file_path}")
    print(f"Full text word count: {final_word_count} words")

# This special check ensures our function only runs if we run this script directly
if __name__ == "__main__":
    generate_mock_data()
