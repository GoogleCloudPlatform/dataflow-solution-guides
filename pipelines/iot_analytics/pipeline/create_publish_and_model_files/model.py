import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from sklearn.model_selection import train_test_split
from sklearn.linear_model import LogisticRegression
import pickle

def create_sample_data(num_samples):
    data = {
        'vehicle_id': [],
        'max_temperature': [],
        'max_vibration': [],
        'last_service_date': [],
        'needs_maintenance': []
    }
    
    for i in range(num_samples):
        vehicle_id = str(1000 + i)
        max_temperature = np.random.randint(50, 100)
        max_vibration = np.random.uniform(0, 1)
        last_service_date = datetime.now() - timedelta(days=np.random.randint(0, 365))
        last_service_date_str = last_service_date.strftime('%Y-%m-%d')
        
        needs_maintenance = (max_temperature > 75) or (max_vibration > 0.5) or (last_service_date < datetime.now() - timedelta(days=180))
        
        data['vehicle_id'].append(vehicle_id)
        data['max_temperature'].append(max_temperature)
        data['max_vibration'].append(max_vibration)
        data['last_service_date'].append(last_service_date_str)
        data['needs_maintenance'].append(needs_maintenance)
    
    return pd.DataFrame(data)

# Create a sample dataset with 100 samples
df = create_sample_data(100)
print(df.head(n=10).to_markdown())

# Convert the last_service_date to a datetime object
df['last_service_date'] = pd.to_datetime(df['last_service_date'])

# Features and target variable
X = df[['max_temperature', 'max_vibration', 'last_service_date']]
y = df['needs_maintenance'].astype(int)

# Convert last_service_date to numeric for modeling
X['last_service_date'] = (X['last_service_date'] - X['last_service_date'].min()).dt.days

# Split the dataset
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

# Create and train the model
model = LogisticRegression()
model.fit(X_train, y_train)

# Save the model to a local file
with open('maintenance_model.pkl', 'wb') as f:
    print("Added Model")
    pickle.dump(model, f)