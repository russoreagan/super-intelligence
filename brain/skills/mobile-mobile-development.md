# Mobile Development (Unified)

## Goal
Build high-quality mobile applications using cross-platform or native frameworks with proper architecture and platform conventions.

## When to Use
- Building cross-platform mobile apps
- Developing native iOS or Android apps
- Implementing mobile navigation
- Managing mobile app state
- Optimizing mobile performance
- Following platform design guidelines

## Framework Comparison

| Framework     | Language          | Performance | Code Sharing | Best For                |
| ------------- | ----------------- | ----------- | ------------ | ----------------------- |
| React Native  | JavaScript/TS     | Near-native | ~95%         | JS teams, rapid dev     |
| Flutter       | Dart              | Near-native | ~95%         | Custom UI, animations   |
| SwiftUI       | Swift             | Native      | iOS only     | iOS-first apps          |
| Jetpack Compose| Kotlin           | Native      | Android only | Android-first apps      |

## React Native

### Project Setup
```bash
# Create new project with Expo
npx create-expo-app@latest MyApp --template tabs

# Or bare React Native
npx react-native@latest init MyApp
```

### Navigation (React Navigation)
```typescript
// App.tsx
import { NavigationContainer } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';

type RootStackParamList = {
  Home: undefined;
  Details: { itemId: number };
  Profile: { userId: string };
};

const Stack = createNativeStackNavigator<RootStackParamList>();
const Tab = createBottomTabNavigator();

function HomeStack() {
  return (
    <Stack.Navigator>
      <Stack.Screen name="Home" component={HomeScreen} />
      <Stack.Screen 
        name="Details" 
        component={DetailsScreen}
        options={({ route }) => ({ title: `Item ${route.params.itemId}` })}
      />
    </Stack.Navigator>
  );
}

function App() {
  return (
    <NavigationContainer>
      <Tab.Navigator>
        <Tab.Screen name="HomeTab" component={HomeStack} />
        <Tab.Screen name="Settings" component={SettingsScreen} />
      </Tab.Navigator>
    </NavigationContainer>
  );
}
```

### State Management (Zustand)
```typescript
// store/userStore.ts
import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import AsyncStorage from '@react-native-async-storage/async-storage';

interface UserState {
  user: User | null;
  isLoading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

export const useUserStore = create<UserState>()(
  persist(
    (set) => ({
      user: null,
      isLoading: false,
      login: async (email, password) => {
        set({ isLoading: true });
        try {
          const user = await api.login(email, password);
          set({ user, isLoading: false });
        } catch (error) {
          set({ isLoading: false });
          throw error;
        }
      },
      logout: () => set({ user: null }),
    }),
    {
      name: 'user-storage',
      storage: createJSONStorage(() => AsyncStorage),
    }
  )
);
```
