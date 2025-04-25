from flask import Flask, render_template, request, jsonify
from langgraph.graph import StateGraph
from langgraph.graph.message import add_messages
from typing import TypedDict
import pymongo
from openai import OpenAI
import json
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()
api_key = os.getenv("API_KEY")
app = Flask(__name__)

# Setup OpenAI/Grok model
client = OpenAI(api_key=api_key, 
                base_url="https://api.x.ai/v1")

def generate_grok_response(prompt, context=None):
    print("\n" + "="*50)
    print("MODEL THINKING:")
    print(f"PROMPT:\n{prompt}")
    if context:
        print(f"\nCONTEXT PROVIDED:\n{context}")
    
    messages = [{"role": "system", "content": "you are a helpful assistant for analyzing procurement data."}]
    if context:
        messages.append({"role": "assistant", "content": str(context)})
    messages.append({"role": "user", "content": prompt})
    
    completion = client.chat.completions.create(
        model="grok-2-latest",
        temperature=0,
        messages=messages
    )
    
    response = completion.choices[0].message.content
    print(f"\nMODEL RESPONSE:\n{response}")
    print("="*50 + "\n")
    return response

# MongoDB setup with your specific schema
mongo_client = pymongo.MongoClient("mongodb://localhost:27017/")
db = mongo_client["procurement_db"]
collection = db["procurement_data"]

# LangGraph state
class State(TypedDict):
    query: str
    query_type: str  # 'procurement_related' or 'out_of_context'
    mongo_script: dict
    mongo_result: dict
    final_response: str

def classify_query(state: State) -> State:
    print("\n" + "="*30)
    print("CLASSIFYING QUERY")
    print(f"Original query: {state['query']}")
    
    prompt = f"""Analyze this query and determine if it's about procurement data or not. 
Classify as:
1. 'procurement_related' - if it's about purchases, suppliers, items, spending, contracts
2. 'out_of_context' - if it's unrelated to procurement

Our database contains procurement records with these exact fields:
- Creation Date, Purchase Date, Fiscal Year
- Purchase Order Number, Acquisition Type, Acquisition Method
- Department Name, Supplier Code, Supplier Name
- CalCard, Item Name, Item Description
- Quantity, Unit Price, Total Price
- Various date/time breakdowns (Year, Month, Quarter, etc.)

Query: {state['query']}

Return ONLY the classification ('procurement_related' or 'out_of_context')."""
    
    classification = generate_grok_response(prompt).strip().lower()
    print(f"Classification result: {classification}")
    
    if "procurement" in classification or "related" in classification:
        state["query_type"] = "procurement_related"
    else:
        state["query_type"] = "out_of_context"
    
    print(f"Final classification: {state['query_type']}")
    print("="*30 + "\n")
    return state

def generate_mongo_script(state: State) -> State:
    print("\n" + "="*30)
    print("GENERATING MONGO QUERY")
    print(f"Working with query: {state['query']}")
    counting = """
   - Use {{"$count": "field_name"}} for total counts
   - Use {{"$group": {{"_id": "$field"}}}} + {{"$count": "field_name"}} for distinct counts
"""

    first_example = """
    {{
        "pipeline": [
            {{"$match": {{"Year": 2013}}}},
            {{"$count": "total_records"}}
        ]
    }}
    """

    avg_unit = """
    {{
        "pipeline": [
            {{"$group": {{
                "_id": "$Department Name",
                "avg_price": {{"$avg": "$Unit Price"}}
            }}},
            {{"$sort": {{"avg_price": -1}}}}
        ]
    }}

    """

    total_spending = """

    {{
        "pipeline": [
            {{"$match": {{"Fiscal Year": "FY2023"}}}},
            {{"$group": {{
                "_id": "$Supplier Name",
                "total_spend": {{"$sum": "$Total Price"}}
            }}}},
            {{"$sort": {{"total_spend": -1}}}},
            {{"$limit": 10}}
        ]
    }}
    """
    monthly_spending = """
    {{
        "pipeline": [
            {{"$match": {{"Item Name": {{"$regex": "Office Supplies", "$options": "i"}}}}}},
            {{"$group": {{
                "_id": {{"year": "$Year", "month": "$Month"}},
                "monthly_total": {{"$sum": "$Total Price"}}
            }}}},
            {{"$sort": {{"_id.year": 1, "_id.month": 1}}}}
        ]
    }}
    """
    prompt = f"""Generate a PRECISE MongoDB aggregation pipeline (as valid JSON) for procurement data based on this question:
"{state['query']}"

### Database Schema (EXACT field names and types):
1. Dates:
   - Creation Date (string, ISO format)
   - Purchase Date (string, ISO format) 
   - Fiscal Year (string, e.g., "FY2023")

2. Order Information:
   - Purchase Order Number (string)
   - Acquisition Type (string)
   - Acquisition Method (string)

3. Department:
   - Department Name (string)

4. Supplier:
   - Supplier Code (float)
   - Supplier Name (string)
   - CalCard (string)

5. Items:
   - Item Name (string)
   - Item Description (string)

6. Financials:
   - Quantity (float)
   - Unit Price (float)
   - Total Price (float)

7. Time Analysis (pre-computed):
   - Year (integer)
   - Month (integer, 1-12)
   - Quarter (integer, 1-4)

### Query Generation Rules:
1. MUST return ONLY a JSON object with "pipeline" key containing the aggregation stages
2. For time filters:
   - Use "Year" for exact year matches (integer)
   - Use "Fiscal Year" for fiscal year matches (string)
   - Use "Month" or "Quarter" for time period analysis
3. For numerical calculations:
   - Use $sum, $avg, $max, $min with proper field names
   - Always specify "_id" in $group stage
4. For counting:
{counting}

### Example Pipelines:

1. Count all records in 2013:
{first_example}

2. Average unit price by department:
{avg_unit}

3. Total spending by supplier in FY2023:
{total_spending}

4. Monthly spending trend for Office Supplies:
{monthly_spending}

### Your Task:
Generate the EXACT MongoDB aggregation pipeline JSON for this query:
"{state['query']}"

Return ONLY the JSON with "pipeline" array, no explanations or markdown formatting."""

    response = generate_grok_response(prompt)
    
    print(f"Raw model response: {response}")
    
    try:
        # Clean and parse the response
        import json
        clean_response = response.strip().replace("```json", "").replace("```", "").strip()
        query_obj = json.loads(clean_response)
        
        if not isinstance(query_obj.get("pipeline"), list):
            raise ValueError("Response must contain a 'pipeline' array")
            
        # Validate pipeline stages
        for stage in query_obj["pipeline"]:
            if not isinstance(stage, dict):
                raise ValueError("Each pipeline stage must be an object")
                
        print(f"Valid MongoDB aggregation generated: {query_obj['pipeline']}")
        state["mongo_script"] = query_obj["pipeline"]
        
    except Exception as e:
        print(f"Error in query generation: {str(e)}")
        state["mongo_script"] = {"error": f"Failed to generate MongoDB query: {str(e)}"}
    
    print("="*30 + "\n")
    return state


def execute_mongo_query(state: State) -> State:
    print("\n" + "="*30)
    print("EXECUTING MONGO QUERY")
    print(f"Query to execute: {state['mongo_script']}")
    
    mongo_query = state["mongo_script"]
    
    if isinstance(mongo_query, list):  # Now handling aggregation pipelines
        try:
            result = list(collection.aggregate(mongo_query))
            print(f"Aggregation executed successfully. Result: {result}")
            state["mongo_result"] = result if result else {"error": "No matching procurement found"}
        except Exception as e:
            print(f"MongoDB execution error: {str(e)}")
            state["mongo_result"] = {"error": f"MongoDB error: {str(e)}"}
    elif isinstance(mongo_query, dict) and "error" not in mongo_query:
        try:
            result = collection.find_one(mongo_query)
            print(f"Query executed successfully. Result: {result}")
            state["mongo_result"] = result or {"error": "No matching procurement found"}
        except Exception as e:
            print(f"MongoDB execution error: {str(e)}")
            state["mongo_result"] = {"error": f"MongoDB error: {str(e)}"}
    else:
        print("Invalid query - skipping execution")
        state["mongo_result"] = {"error": "Invalid Mongo query - not executed"}
    
    print("="*30 + "\n")
    return state

def generate_response(state: State) -> State:
    print("\n" + "="*30)
    print("GENERATING FINAL RESPONSE")
    print(f"Original query: {state['query']}")
    print(f"Available context: {state.get('mongo_result', {})}")
    
    context = state.get("mongo_result", {})
    
    if state["query_type"] == "out_of_context":
        prompt = f"""The user asked this non-procurement question:
{state['query']}

Provide a helpful but brief response explaining this system handles procurement data."""
    else:
        prompt = f"""The user asked this procurement-related question:
{state['query']}

Database results: {context}

Generate a detailed response that:
1. Directly answers the question
2. Formats numbers properly (e.g., $46.56)
3. Includes all relevant fields from the result
4. Explains if no matching procurement was found
5. Is professional and clear

Example good response:
"Found a matching procurement record:
- Item: tape
- Supplier: NATIONAL OFFICE SOLUTIONS
- Purchase Date: June 26, 2012
- Total Price: $46.56
- Department: Developmental Services" """
    
    state["final_response"] = generate_grok_response(prompt, context).strip()
    
    print(f"Final response prepared: {state['final_response']}")
    print("="*30 + "\n")
    return state

# Build LangGraph
builder = StateGraph(State)

builder.add_node("classify", classify_query)
builder.add_node("generate_mongo_script", generate_mongo_script)
builder.add_node("execute_mongo", execute_mongo_query)
builder.add_node("respond", generate_response)

builder.set_entry_point("classify")

def route_classification(state: State) -> str:
    print("\n" + "="*30)
    print("ROUTING DECISION")
    print(f"Routing based on query type: {state['query_type']}")
    print("="*30 + "\n")
    return state["query_type"]

builder.add_conditional_edges(
    "classify",
    route_classification,
    {
        "out_of_context": "respond",
        "procurement_related": "generate_mongo_script"
    }
)

builder.add_edge("generate_mongo_script", "execute_mongo")
builder.add_edge("execute_mongo", "respond")

builder.set_finish_point("respond")
graph = builder.compile()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/chat', methods=['GET'])
def chat():
    user_message = request.args.get('message')
    
    if not user_message:
        return jsonify({"response": "Please provide a message"})
    
    # Process the message through the LangGraph pipeline
    result = graph.invoke({"query": user_message})
    
    # Return the final response
    return jsonify({"response": result["final_response"]})

if __name__ == '__main__':
    app.run(debug=True)